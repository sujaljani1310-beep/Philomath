import asyncio
import json
import logging
import random
import unittest
from dataclasses import replace
from tempfile import SpooledTemporaryFile
from threading import Event
from unittest.mock import patch

import httpx
from fastapi import FastAPI, File, Form, UploadFile

from app.ingress import IngressLimits, IngressMiddleware, _MultipartBudget


def multipart(parts, boundary=b"synthetic-boundary"):
    result = bytearray()
    for name, filename, value in parts:
        result.extend(b"--" + boundary + b"\r\nContent-Disposition: form-data; name=\"" + name + b"\"")
        if filename is not None:
            result.extend(b"; filename=\"" + filename + b"\"")
        result.extend(b"\r\n\r\n" + value + b"\r\n")
    result.extend(b"--" + boundary + b"--\r\n")
    return bytes(result)


CONTENT_TYPE = (b"content-type", b"multipart/form-data; boundary=synthetic-boundary")


class IngressTests(unittest.IsolatedAsyncioTestCase):
    async def run_request(self, body=b"", *, path="/api/chat/send", headers=(), limits=None,
                          chunks=None, messages=None, body_timeout=1, app=None, method="POST"):
        output, called = [], []
        incoming = list(messages if messages is not None else [
            {"type": "http.request", "body": chunk, "more_body": index < len(chunks or [body]) - 1}
            for index, chunk in enumerate(chunks or [body])
        ])

        async def receive():
            return incoming.pop(0) if incoming else {"type": "http.disconnect"}

        async def send(message):
            output.append(message)

        async def echo(scope, read, write):
            called.append(True)
            received = bytearray()
            while True:
                message = await read()
                received.extend(message.get("body", b""))
                if not message.get("more_body"):
                    break
            await write({"type": "http.response.start", "status": 200, "headers": []})
            await write({"type": "http.response.body", "body": bytes(received)})

        await IngressMiddleware(app or echo, limits=limits, body_timeout=body_timeout)(
            {"type": "http", "path": path, "method": method, "headers": list(headers)}, receive, send
        )
        status = output[0]["status"] if output else None
        payload = b"".join(message.get("body", b"") for message in output)
        return status, payload, called, incoming

    async def test_body_byte_boundaries_without_content_length_and_chunked(self):
        limits = replace(IngressLimits(), chat_body_bytes=8)
        for headers in ([], [(b"transfer-encoding", b"chunked")]):
            with self.subTest(headers=headers):
                status, payload, called, _ = await self.run_request(chunks=[b"1234", b"5678"], headers=headers, limits=limits)
                self.assertEqual((status, payload, called), (200, b"12345678", [True]))
                status, _, called, _ = await self.run_request(chunks=[b"1234", b"56789"], headers=headers, limits=limits)
                self.assertEqual((status, called), (413, []))

    async def test_path_limits_and_trailing_slash(self):
        limits = replace(IngressLimits(), health_body_bytes=2, api_body_bytes=3, chat_body_bytes=4)
        for path, size in [("/", 2), ("/api/integrations", 3), ("/api/chat/send", 4), ("/api/chat/send/", 4)]:
            with self.subTest(path=path):
                self.assertEqual((await self.run_request(b"x" * size, path=path, limits=limits))[0], 200)
                self.assertEqual((await self.run_request(b"x" * (size + 1), path=path, limits=limits))[0], 413)

    async def test_invalid_duplicate_and_conflicting_content_length_rejected_before_read(self):
        malformed = [
            [(b"content-length", b"-1")], [(b"content-length", b"+1")],
            [(b"content-length", b" 1")], [(b"content-length", b"1, 1")],
            [(b"content-length", b"")], [(b"content-length", b"9" * 21)],
            [(b"content-length", b"1"), (b"content-length", b"1")],
            [(b"content-length", b"1"), (b"content-length", b"2")],
            [(b"content-length", b"1"), (b"transfer-encoding", b"chunked")],
        ]
        for headers in malformed:
            with self.subTest(headers=headers):
                status, _, called, unread = await self.run_request(b"x", headers=headers)
                self.assertEqual((status, called, len(unread)), (422, [], 1))
        status, _, called, unread = await self.run_request(
            b"x", headers=[(b"content-length", b"5")], limits=replace(IngressLimits(), chat_body_bytes=4)
        )
        self.assertEqual((status, called, len(unread)), (413, [], 1))

    async def test_content_length_is_not_trusted_and_must_match_actual_body(self):
        for declared, actual in [(0, b"x"), (2, b"x")]:
            self.assertEqual((await self.run_request(actual, headers=[(b"content-length", str(declared).encode())]))[0], 422)
        status, _, called, _ = await self.run_request(
            b"12345", headers=[(b"content-length", b"0")], limits=replace(IngressLimits(), chat_body_bytes=4)
        )
        self.assertEqual((status, called), (413, []))

    async def test_multipart_file_and_aggregate_boundaries_on_one_byte_chunks(self):
        limits = replace(IngressLimits(), file_bytes=4, aggregate_file_bytes=6)
        body = multipart([(b"files", b"one.txt", b"1234"), (b"files", b"two.txt", b"56")])
        status, payload, called, _ = await self.run_request(
            path="/api/files/upload", headers=[CONTENT_TYPE], chunks=[bytes([byte]) for byte in body], limits=limits
        )
        self.assertEqual((status, payload, called), (200, body, [True]))
        for parts in [
            [(b"files", b"one.txt", b"12345")],
            [(b"files", b"one.txt", b"1234"), (b"files", b"two.txt", b"567")],
        ]:
            with self.subTest(parts=parts):
                status, _, called, _ = await self.run_request(multipart(parts), path="/api/files/upload", headers=[CONTENT_TYPE], limits=limits)
                self.assertEqual((status, called), (413, []))

    async def test_file_and_field_counts_include_empty_parts(self):
        limits = replace(IngressLimits(), files=2, fields=2)
        for is_file in (True, False):
            name = b"files" if is_file else b"note"
            filename = b"" if is_file else None
            for count, expected in [(2, 200), (3, 413)]:
                with self.subTest(is_file=is_file, count=count):
                    response = await self.run_request(
                        multipart([(name, filename, b"")] * count), path="/api/files/upload", headers=[CONTENT_TYPE], limits=limits
                    )
                    self.assertEqual(response[0], expected)

    async def test_field_and_header_bytes_are_bounded(self):
        limits = replace(IngressLimits(), field_bytes=4, part_header_bytes=128)
        for size, expected in [(4, 200), (5, 413)]:
            self.assertEqual((await self.run_request(
                multipart([(b"note", None, b"x" * size)]), path="/api/files/upload", headers=[CONTENT_TYPE], limits=limits
            ))[0], expected)
        for oversized in (
            b"X-" + b"x" * 200 + b": y\r\n",
            b"X-Long: " + b"x" * 200 + b"\r\n",
            b"X-One: " + b"x" * 70 + b"\r\nX-Two: " + b"x" * 70 + b"\r\n",
            b"X-One:" + b" " * 70 + b"\r\nX-Two:" + b" " * 70 + b"\r\n",
        ):
            body = multipart([(b"files", b"test.txt", b"x")]).replace(b"\r\n\r\n", b"\r\n" + oversized + b"\r\n", 1)
            status, _, called, _ = await self.run_request(body, path="/api/files/upload", headers=[CONTENT_TYPE], limits=limits)
            self.assertEqual((status, called), (413, []))

    async def test_oversized_asgi_chunk_is_rejected_before_parser_or_spool(self):
        limits = replace(IngressLimits(), upload_body_bytes=3)
        spool = SpooledTemporaryFile(mode="w+b")
        try:
            with patch("app.ingress.SpooledTemporaryFile", return_value=spool), patch.object(_MultipartBudget, "write") as parse, patch("app.ingress._spool_io") as io:
                status, _, called, _ = await self.run_request(b"1234", path="/api/files/upload", headers=[CONTENT_TYPE], limits=limits)
        finally:
            spool.close()
        self.assertEqual((status, called), (413, []))
        parse.assert_not_called()
        # Closing an unused spool is still required.
        self.assertEqual(io.call_count, 1)

    async def test_malformed_multipart_is_sanitized_and_never_reaches_app(self):
        bodies = [b"private-malicious-name", multipart([(b"files", b"secret.txt", b"x")])[:-30]]
        for body in bodies:
            with self.subTest(body=body):
                status, payload, called, _ = await self.run_request(body, path="/api/files/upload", headers=[CONTENT_TYPE])
                self.assertEqual((status, called), (422, []))
                self.assertEqual(json.loads(payload), {"detail": "Invalid request body."})
        for content_type in (b"multipart/form-data", b"multipart/form-data; boundary=", b"text/plain"):
            self.assertEqual((await self.run_request(b"x", path="/api/files/upload", headers=[(b"content-type", content_type)]))[0], 422)

    async def test_parser_diagnostics_cannot_invoke_synchronous_library_handlers(self):
        library_logger = logging.getLogger("python_multipart.multipart")
        with patch.object(library_logger, "warning", side_effect=RuntimeError("library handler must not run")) as warn:
            status, _, called, _ = await self.run_request(b"malformed", path="/api/files/upload", headers=[CONTENT_TYPE])
        self.assertEqual((status, called), (422, []))
        warn.assert_not_called()

    async def test_randomized_binary_multipart_chunk_boundaries(self):
        rng = random.Random(1203)
        for _ in range(128):
            file_bytes = bytes(rng.randrange(256) for _ in range(rng.randrange(512)))
            body = multipart([(b"files", b"synthetic.txt", file_bytes)])
            budget = _MultipartBudget(CONTENT_TYPE[1], IngressLimits())
            offset = 0
            while offset < len(body):
                end = offset + rng.randrange(1, 80)
                budget.write(body[offset:end])
                offset = end
            budget.finish()
            self.assertEqual((budget.file_count, budget.file_total), (1, len(file_bytes)))

    async def test_valid_multipart_replays_unchanged_to_real_fastapi_parser(self):
        app = FastAPI()

        @app.post("/api/files/upload")
        async def upload(files: list[UploadFile] = File(...), note: str = Form("")):
            result = [{"name": file.filename, "value": (await file.read()).decode()} for file in files]
            for file in files:
                await file.close()
            return {"files": result, "note": note}

        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=IngressMiddleware(app)), base_url="https://synthetic.invalid") as client:
            response = await client.post("/api/files/upload", files=[("files", ("one.txt", b"hello")), ("files", ("two.txt", b"world"))], data={"note": "hello"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"files": [{"name": "one.txt", "value": "hello"}, {"name": "two.txt", "value": "world"}], "note": "hello"})

    async def test_options_does_not_require_multipart(self):
        self.assertEqual((await self.run_request(path="/api/files/upload", method="OPTIONS"))[0], 200)
        limits = replace(IngressLimits(), api_body_bytes=3, upload_body_bytes=100)
        for method in ("OPTIONS", "GET", "PUT", "DELETE"):
            with self.subTest(method=method):
                status, _, called, _ = await self.run_request(
                    b"1234", path="/api/files/upload", method=method, limits=limits
                )
                self.assertEqual((status, called), (413, []))

    async def test_disk_rollover_disconnect_and_rejection_close_spools(self):
        spools = []

        def create(**kwargs):
            spool = SpooledTemporaryFile(**kwargs)
            spools.append(spool)
            return spool

        with patch("app.ingress.SpooledTemporaryFile", side_effect=create):
            result = await self.run_request(b"x" * 100, limits=replace(IngressLimits(), spool_memory_bytes=8))
            self.assertEqual(result[0], 200)
            self.assertTrue(spools[-1]._rolled)
            result = await self.run_request(messages=[{"type": "http.request", "body": b"x", "more_body": True}, {"type": "http.disconnect"}])
            self.assertEqual((result[0], result[2]), (None, []))
            self.assertEqual((await self.run_request(b"xx", limits=replace(IngressLimits(), chat_body_bytes=1)))[0], 413)
        self.assertTrue(all(spool.closed for spool in spools))

    async def test_slow_body_has_total_deadline_and_closes_spool(self):
        spools = []

        def create(**kwargs):
            spool = SpooledTemporaryFile(**kwargs)
            spools.append(spool)
            return spool

        async def slow_receive():
            await asyncio.sleep(1)
            return {"type": "http.request", "body": b"x", "more_body": True}

        called, output = [], []

        async def app(*args):
            called.append(True)

        async def send(message):
            output.append(message)

        with patch("app.ingress.SpooledTemporaryFile", side_effect=create):
            await IngressMiddleware(app, body_timeout=.01)({"type": "http", "path": "/api/chat/send", "headers": []}, slow_receive, send)
        self.assertEqual((output[0]["status"], called), (504, []))
        self.assertTrue(spools[0].closed)

    async def test_empty_chunk_stream_cannot_starve_body_deadline(self):
        output = []

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": True}

        async def app(*args):
            self.fail("unbounded empty body reached app")

        async def send(message):
            output.append(message)

        await IngressMiddleware(app, body_timeout=.01)(
            {"type": "http", "path": "/api/chat/send", "method": "POST", "headers": []}, receive, send
        )
        self.assertEqual(output[0]["status"], 504)

    async def test_spool_storage_failure_is_sanitized_and_closed(self):
        spool = SpooledTemporaryFile(mode="w+b")
        with patch("app.ingress.SpooledTemporaryFile", return_value=spool), patch.object(
            spool, "write", side_effect=OSError("private-storage-path")
        ):
            status, payload, called, _ = await self.run_request(b"hello")
        self.assertEqual((status, called), (503, []))
        self.assertNotIn(b"private-storage-path", payload)
        self.assertTrue(spool.closed)

    async def test_upload_body_has_separate_bounded_budget(self):
        body = multipart([(b"files", b"sample.txt", b"hello")])

        async def run(path, method, *, body_timeout=.01, upload_timeout=.2):
            output = []
            sent = False

            async def receive():
                nonlocal sent
                if sent:
                    return {"type": "http.disconnect"}
                sent = True
                await asyncio.sleep(.025)
                return {"type": "http.request", "body": body}

            async def app(scope, receive, send):
                message = await receive()
                self.assertEqual(message["body"], body)
                await send({"type": "http.response.start", "status": 200, "headers": []})
                await send({"type": "http.response.body", "body": b"ok"})

            async def send(message):
                output.append(message)

            await IngressMiddleware(app, body_timeout=body_timeout, upload_body_timeout=upload_timeout)(
                {"type": "http", "path": path, "method": method, "headers": [CONTENT_TYPE]}, receive, send
            )
            return output[0]["status"]

        self.assertEqual(await run("/api/files/upload", "POST"), 200)
        self.assertEqual(await run("/api/chat/send", "POST"), 504)
        self.assertEqual(await run("/api/files/upload", "OPTIONS"), 504)
        self.assertEqual(await run("/api/files/upload", "POST", upload_timeout=.01), 504)

    async def test_cancelled_body_cleans_up_without_response(self):
        started = asyncio.Event()
        spools, output = [], []

        def create(**kwargs):
            spool = SpooledTemporaryFile(**kwargs)
            spools.append(spool)
            return spool

        async def receive():
            started.set()
            await asyncio.Event().wait()

        async def app(*args):
            self.fail("cancelled body reached app")

        async def send(message):
            output.append(message)

        with patch("app.ingress.SpooledTemporaryFile", side_effect=create):
            task = asyncio.create_task(IngressMiddleware(app)({"type": "http", "path": "/api/chat/send", "headers": []}, receive, send))
            await started.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertEqual(output, [])
        self.assertTrue(spools[0].closed)

    async def test_cancelled_worker_finishes_before_spool_close(self):
        started, release, finished = Event(), Event(), Event()
        real_spool = SpooledTemporaryFile(max_size=8, mode="w+b")
        original_write = real_spool.write

        def write(data):
            started.set()
            if not release.wait(2):
                raise RuntimeError("test worker was not released")
            original_write(data)
            finished.set()

        original_close = real_spool.close

        def close():
            self.assertTrue(finished.is_set())
            original_close()

        with patch("app.ingress.SpooledTemporaryFile", return_value=real_spool), patch.object(real_spool, "write", side_effect=write), patch.object(real_spool, "close", side_effect=close):
            task = asyncio.create_task(self.run_request(b"hello"))
            try:
                for _ in range(100):
                    if started.is_set():
                        break
                    await asyncio.sleep(.001)
                self.assertTrue(started.is_set())
                task.cancel()
                await asyncio.sleep(.005)
                self.assertFalse(task.done())
                task.cancel()
                await asyncio.sleep(.005)
                self.assertFalse(task.done())
                self.assertFalse(real_spool.closed)
            finally:
                release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertTrue(real_spool.closed)

    async def test_concurrent_spools_never_mix_request_bodies(self):
        bodies = [(f"synthetic-user-{number}|".encode() * 1024) for number in range(12)]
        results = await asyncio.gather(*[self.run_request(body, limits=replace(IngressLimits(), spool_memory_bytes=8)) for body in bodies])
        for body, result in zip(bodies, results):
            self.assertEqual(result[:3], (200, body, [True]))

    async def test_downstream_exception_is_not_reclassified_as_body_timeout(self):
        async def app(*args):
            raise TimeoutError("synthetic downstream timeout")

        with self.assertRaisesRegex(TimeoutError, "synthetic downstream timeout"):
            await self.run_request(app=app)

    async def test_lifespan_bypasses_body_handling(self):
        seen = []

        async def app(scope, receive, send):
            seen.append(scope)

        scope = {"type": "lifespan"}
        await IngressMiddleware(app)(scope, None, None)
        self.assertEqual(seen, [scope])
