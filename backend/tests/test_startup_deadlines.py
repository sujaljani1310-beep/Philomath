"""Socket-free startup/auth regression tests using synthetic users and services."""

import asyncio
import gzip
import json
import time
import unittest
import zlib
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import httpx
from fastapi import HTTPException

from app.http_safety import stateless_cookies
from app.services import auth_service, startup_service as startup


class TrackingStream(httpx.AsyncByteStream):
    def __init__(self, chunks, pause=0):
        self.chunks = chunks
        self.pause = pause
        self.closed = False
        self.reads = 0

    async def __aiter__(self):
        for chunk in self.chunks:
            self.reads += 1
            if self.pause:
                await asyncio.sleep(self.pause)
            yield chunk

    async def aclose(self):
        self.closed = True


class StartupReadTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.requests = []
        self.handler = lambda request: httpx.Response(200, json={"id": "owner"})

        async def handle(request):
            self.requests.append(request)
            result = self.handler(request)
            if asyncio.iscoroutine(result):
                return await result
            return result

        self.http = httpx.AsyncClient(
            transport=httpx.MockTransport(handle), cookies=stateless_cookies(),
            event_hooks={"response": [startup._redirect_guard]},
        )
        self.patches = [
            patch.object(startup, "_client", self.http),
            patch.object(startup, "_configuration", return_value=(
                "https://database.example.invalid", "synthetic-service-key",
            )),
            patch("app.request_limits.remaining_startup_seconds", return_value=None),
        ]
        for active in self.patches:
            active.start()
        self.addAsyncCleanup(self.http.aclose)
        self.addCleanup(lambda: [active.stop() for active in reversed(self.patches)])

    async def test_every_request_remotely_validates_token_and_headers_are_separate(self):
        for _ in range(2):
            user = await startup.get_startup_user("Bearer synthetic-user-token")
            self.assertEqual(user.id, "owner")
        self.assertEqual(len(self.requests), 2)
        for request in self.requests:
            self.assertEqual(request.headers["authorization"], "Bearer synthetic-user-token")
            self.assertEqual(request.headers["apikey"], "synthetic-service-key")
            self.assertEqual(request.url.path, "/auth/v1/user")
            self.assertNotIn("cookie", request.headers)

    async def test_upload_reuses_only_its_pre_body_validation_and_next_upload_validates_again(self):
        from app import main
        from app.request_limits import get_prevalidated_upload_user

        self.handler = lambda request: httpx.Response(200, json={
            "id": request.headers["authorization"].removeprefix("Bearer "),
        })
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=main.app), base_url="http://asgi.example.invalid",
        ) as caller:
            for token in ("synthetic-upload-one", "synthetic-upload-two"):
                before = len(self.requests)
                response = await caller.post(
                    "/api/files/upload", headers={"Authorization": f"Bearer {token}"},
                    files={"files": ("sample.txt", b"Small synthetic text", "text/plain")},
                )
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.json()["files"][0]["text"], "Small synthetic text")
                self.assertEqual(len(self.requests), before + 1)
                self.assertEqual(self.requests[-1].url.path, "/auth/v1/user")
                self.assertEqual(self.requests[-1].headers["authorization"], f"Bearer {token}")
                self.assertIsNone(get_prevalidated_upload_user())

    async def test_overlapping_users_do_not_share_tokens_cookies_or_database_filters(self):
        arrived = 0
        ready = asyncio.Event()

        async def handle(request):
            nonlocal arrived
            if request.url.path.endswith("/user"):
                arrived += 1
                if arrived == 12:
                    ready.set()
                await asyncio.wait_for(ready.wait(), 1)
                owner = request.headers["authorization"].removeprefix("Bearer ")
                return httpx.Response(200, json={"id": owner}, headers={
                    "set-cookie": f"session={owner}; Path=/; Secure",
                })
            owner = request.url.params["user_id"].removeprefix("eq.")
            return httpx.Response(200, json=[{"id": owner}])

        self.handler = handle

        async def run(index):
            user = await startup.get_startup_user(f"Bearer user-{index}")
            rows = await startup.list_startup_conversations(user.id)
            self.assertEqual(rows, [{"id": f"user-{index}"}])

        await asyncio.gather(*(run(index) for index in range(12)))
        self.assertEqual(len(self.requests), 24)
        for request in self.requests:
            self.assertNotIn("cookie", request.headers)
            if request.url.path.endswith("/conversations"):
                self.assertEqual(request.headers["authorization"], "Bearer synthetic-service-key")
        self.assertEqual(list(self.http.cookies.jar), [])

    async def test_definitive_invalid_credentials_are_401_other_upstream_failures_503(self):
        for status, payload, expected in (
            (401, {"message": "sensitive invalid token"}, 401),
            (403, {"code": "bad_jwt"}, 401),
            (403, {"code": "jwt_expired"}, 401),
            (403, {"message": "upstream proxy unavailable"}, 503),
            (429, {"message": "sensitive upstream limit"}, 503),
            (500, {"message": "sensitive database details"}, 503),
            (503, {"code": "bad_jwt"}, 503),
            (200, None, 503),
            (200, {}, 503),
            (200, {"id": "owner", "email": {}}, 503),
        ):
            with self.subTest(status=status, payload=payload):
                self.handler = lambda request: httpx.Response(status, json=payload)
                with self.assertRaises(HTTPException) as caught:
                    await startup.get_startup_user("Bearer synthetic-token")
                self.assertEqual(caught.exception.status_code, expected)
                self.assertNotIn("sensitive", caught.exception.detail)

    async def test_no_retry_or_retry_after_sleep_on_auth_or_postgrest_outage(self):
        self.handler = lambda request: httpx.Response(
            503, headers={"retry-after": "120"}, json={"message": "outage"},
        )
        for operation in (
            lambda: startup.get_startup_user("Bearer synthetic-token"),
            lambda: startup.list_startup_conversations("owner"),
        ):
            before = len(self.requests)
            with patch("asyncio.sleep", side_effect=AssertionError("no backoff expected")):
                with self.assertRaises(HTTPException) as caught:
                    await operation()
            self.assertEqual(caught.exception.status_code, 503)
            self.assertEqual(len(self.requests), before + 1)

    async def test_malformed_auth_error_codes_are_sanitized_503_including_asgi(self):
        for status in (400, 403, 422):
            for code in ([], {}, ["bad_jwt"], {"secret": "sensitive"}, 1):
                with self.subTest(status=status, code=code):
                    self.assertFalse(auth_service.invalid_auth_response(status, code))
                    self.handler = lambda request: httpx.Response(status, json={"code": code})
                    with self.assertRaises(HTTPException) as caught:
                        await startup.get_startup_user("Bearer synthetic-token")
                    self.assertEqual(caught.exception.status_code, 503)
                    self.assertNotIn("sensitive", caught.exception.detail)
        from app.main import app

        # This part exercises the real ingress deadline, rather than the
        # service-only fixture's standalone (None) deadline override.
        self.patches[-1].stop()
        self.handler = lambda request: httpx.Response(403, json={"code": {"secret": "sensitive"}})
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://asgi.example.invalid",
        ) as caller:
            response = await caller.get("/api/conversations", headers={"Authorization": "Bearer synthetic-token"})
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("sensitive", response.text)
        self.assertTrue(auth_service.invalid_auth_response(401, []))

    async def test_slow_auth_phase_is_cancelled_and_pool_recovers(self):
        cancelled = asyncio.Event()

        async def slow(request):
            try:
                await asyncio.sleep(10)
            finally:
                cancelled.set()

        self.handler = slow
        started = time.perf_counter()
        with patch.object(startup, "STARTUP_PHASE_SECONDS", 0.025):
            with self.assertRaises(HTTPException) as caught:
                await startup.get_startup_user("Bearer synthetic-token")
        self.assertEqual(caught.exception.status_code, 503)
        self.assertTrue(cancelled.is_set())
        self.assertLess(time.perf_counter() - started, 0.5)
        self.handler = lambda request: httpx.Response(200, json={"id": "recovered"})
        self.assertEqual((await startup.get_startup_user("Bearer synthetic-token")).id, "recovered")

    async def test_caller_cancellation_stops_upstream_without_detached_work(self):
        entered = asyncio.Event()
        stopped = asyncio.Event()

        async def slow(request):
            entered.set()
            try:
                await asyncio.sleep(10)
            finally:
                stopped.set()

        self.handler = slow
        task = asyncio.create_task(startup.get_startup_user("Bearer synthetic-token"))
        await asyncio.wait_for(entered.wait(), 1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(stopped.is_set())
        self.assertEqual(len(self.requests), 1)

    async def test_auth_and_database_share_remaining_deadline_instead_of_resetting_it(self):
        async def handle(request):
            await asyncio.sleep(0.035)
            payload = {"id": "owner"} if request.url.path.endswith("/user") else []
            return httpx.Response(200, json=payload)

        self.handler = handle
        deadline = time.monotonic() + 0.055
        with patch("app.request_limits.remaining_startup_seconds", side_effect=lambda: deadline - time.monotonic()):
            user = await startup.get_startup_user("Bearer synthetic-token")
            with self.assertRaises(HTTPException) as caught:
                await startup.list_startup_conversations(user.id)
        self.assertEqual(caught.exception.status_code, 504)
        self.assertEqual(len(self.requests), 2)

    async def test_streamed_response_size_and_time_limits_close_connections(self):
        for chunks, pause, cap, timeout in (
            ([b"x" * 9, b"x" * 9], 0, 16, 1),
            ([b"{", b'"id":"owner"}'], 0.03, 100, 0.02),
        ):
            stream = TrackingStream(chunks, pause=pause)
            self.handler = lambda request: httpx.Response(200, stream=stream)
            with patch.object(startup, "AUTH_RESPONSE_BYTES", cap), patch.object(
                startup, "STARTUP_PHASE_SECONDS", timeout,
            ):
                with self.assertRaises(HTTPException) as caught:
                    await startup.get_startup_user("Bearer synthetic-token")
            self.assertEqual(caught.exception.status_code, 503)
            self.assertTrue(stream.closed)

    async def test_bounded_gzip_wrapped_and_raw_deflate_preserve_valid_responses(self):
        payload = json.dumps({"id": "owner", "email": "synthetic@example.invalid"}).encode()
        raw = zlib.compressobj(wbits=-zlib.MAX_WBITS)
        for encoding, compressed in (
            ("gzip", gzip.compress(payload)),
            ("deflate", zlib.compress(payload)),
            ("deflate", raw.compress(payload) + raw.flush()),
        ):
            stream = TrackingStream([compressed[:1], compressed[1:]])
            self.handler = lambda request: httpx.Response(
                200, headers={"content-encoding": encoding}, stream=stream,
            )
            user = await startup.get_startup_user("Bearer synthetic-token")
            self.assertEqual(user.id, "owner")
            self.assertEqual(user.email, "synthetic@example.invalid")
            self.assertTrue(stream.closed)
            self.assertEqual(self.requests[-1].headers["accept-encoding"], "gzip, deflate")

    async def test_raw_deflate_that_resembles_zlib_header_preserves_httpx_fallback(self):
        raw = b"\x78\x9c\x00\x63\xff" + b"x" * 156 + b"\x01\x00\x00\xff\xff"
        self.assertEqual(zlib.decompress(raw, -zlib.MAX_WBITS), b"x" * 156)
        for chunks in ([raw], [raw[:1], raw[1:]], [raw[:64], raw[64:]]):
            stream = TrackingStream(chunks)
            response = httpx.Response(200, headers={"content-encoding": "deflate"}, stream=stream)
            try:
                self.assertEqual(await startup._bounded_response_content(response, 156), b"x" * 156)
            finally:
                await response.aclose()
            self.assertTrue(stream.closed)
        stream = TrackingStream([raw[:1], raw[1:]])
        response = httpx.Response(200, headers={"content-encoding": "deflate"}, stream=stream)
        try:
            with self.assertRaises(ValueError):
                await startup._bounded_response_content(response, 155)
        finally:
            await response.aclose()

    async def test_compression_bomb_is_rejected_before_unbounded_expansion(self):
        compressed = gzip.compress(b'{"id":"' + b"x" * 2_000_000 + b'"}')
        stream = TrackingStream([compressed])
        self.handler = lambda request: httpx.Response(
            200, headers={"content-encoding": "gzip"}, stream=stream,
        )
        actual_factory = zlib.decompressobj
        budgets = []

        class BoundedDecoder:
            def __init__(self, *args):
                self.decoder = actual_factory(*args)

            def decompress(self, chunk, max_length):
                budgets.append(max_length)
                return self.decoder.decompress(chunk, max_length)

            @property
            def eof(self):
                return self.decoder.eof

        with patch.object(startup, "AUTH_RESPONSE_BYTES", 128), patch.object(
            startup.zlib, "decompressobj", BoundedDecoder,
        ):
            with self.assertRaises(HTTPException) as caught:
                await startup.get_startup_user("Bearer synthetic-token")
        self.assertEqual(caught.exception.status_code, 503)
        self.assertEqual(budgets, [129])
        self.assertTrue(stream.closed)

    async def test_compressed_wire_bytes_truncation_and_unsupported_encoding_fail_closed(self):
        gzip_payload = gzip.compress(b'{"id":"owner"}')
        # A valid but excessive optional gzip filename must not bypass the
        # encoded-byte cap merely because it expands to almost no output.
        oversized_header = gzip_payload[:3] + b"\x08" + gzip_payload[4:10] + b"x" * 70_000 + b"\x00" + gzip_payload[10:]
        for encoding, payload in (
            ("gzip", oversized_header),
            ("gzip", gzip.compress(b'{"id":"owner"}')[:-2]),
            ("deflate", zlib.compress(b'{"id":"owner"}')[:-2]),
            ("br", b'{"id":"owner"}'),
        ):
            stream = TrackingStream([payload])
            self.handler = lambda request: httpx.Response(
                200, headers={"content-encoding": encoding}, stream=stream,
            )
            with patch.object(startup, "AUTH_RESPONSE_BYTES", 64):
                with self.assertRaises(HTTPException) as caught:
                    await startup.get_startup_user("Bearer synthetic-token")
            self.assertEqual(caught.exception.status_code, 503)
            self.assertTrue(stream.closed)
            self.assertNotIn("gzip", caught.exception.detail)

    async def test_trailing_junk_and_multiple_compressed_members_are_rejected(self):
        payload = b'{"id":"owner"}'
        for encoding, compressed in (
            ("gzip", gzip.compress(payload) + b"trailing junk"),
            ("gzip", gzip.compress(payload) + gzip.compress(b'{"id":"other"}')),
            ("deflate", zlib.compress(payload) + b"trailing junk"),
        ):
            stream = TrackingStream([compressed[:1], compressed[1:]])
            self.handler = lambda request: httpx.Response(
                200, headers={"content-encoding": encoding}, stream=stream,
            )
            with self.assertRaises(HTTPException) as caught:
                await startup.get_startup_user("Bearer synthetic-token")
            self.assertEqual(caught.exception.status_code, 503)
            self.assertTrue(stream.closed)
            self.assertNotIn("trailing", caught.exception.detail)

    async def test_redirects_never_forward_credentials_cross_origin_or_to_http(self):
        for status in (301, 302, 307, 308):
            for destination in (
                "https://untrusted.example.invalid/user",
                "http://database.example.invalid/user",
                "https://database.example.invalid:8443/user",
            ):
                with self.subTest(status=status, destination=destination):
                    before = len(self.requests)
                    self.handler = lambda request: httpx.Response(status, headers={"location": destination})
                    with self.assertRaises(HTTPException) as caught:
                        await startup.get_startup_user("Bearer synthetic-token")
                    self.assertEqual(caught.exception.status_code, 503)
                    self.assertEqual(len(self.requests), before + 1)
                    self.assertNotIn("untrusted", caught.exception.detail)

    async def test_same_https_origin_redirect_closes_body_without_reading_it(self):
        stream = TrackingStream([b"x" * 1000])

        def handle(request):
            if request.url.path.endswith("/user"):
                return httpx.Response(307, headers={"location": "/validated-user"}, stream=stream)
            return httpx.Response(200, json={"id": "owner"})

        self.handler = handle
        self.assertEqual((await startup.get_startup_user("Bearer synthetic-token")).id, "owner")
        self.assertTrue(stream.closed)
        self.assertEqual(stream.reads, 0)
        self.assertEqual(len(self.requests), 2)
        self.assertEqual(self.requests[-1].headers["authorization"], "Bearer synthetic-token")

    async def test_redirect_loop_is_bounded_within_phase(self):
        self.handler = lambda request: httpx.Response(308, headers={"location": "/loop"})
        with self.assertRaises(HTTPException) as caught:
            await startup.get_startup_user("Bearer synthetic-token")
        self.assertEqual(caught.exception.status_code, 503)
        self.assertEqual(len(self.requests), 21)

    async def test_list_projection_order_and_ownership_are_unchanged(self):
        self.handler = lambda request: httpx.Response(200, json=[{"id": "row"}])
        self.assertEqual(await startup.list_startup_conversations("owner"), [{"id": "row"}])
        self.assertEqual(await startup.list_startup_integrations("owner"), [{"id": "row"}])
        conversations, integrations = self.requests
        self.assertEqual(conversations.url.params["select"], "*")
        self.assertEqual(conversations.url.params["order"], "updated_at.desc")
        self.assertEqual(integrations.url.params["order"], "created_at.asc")
        self.assertEqual(integrations.url.params["select"], "id, provider, display_name, api_key_last4, created_at, updated_at")
        self.assertNotIn("encrypted_api_key", integrations.url.params["select"])
        for request in self.requests:
            self.assertEqual(request.url.params["user_id"], "eq.owner")
            self.assertEqual(request.headers["authorization"], "Bearer synthetic-service-key")
            self.assertEqual(request.headers["accept-profile"], "public")

    async def test_bad_database_payload_fails_safely_without_retry(self):
        for payload in ({"secret": "upstream error"}, ["not a row"], None):
            self.handler = lambda request: httpx.Response(200, json=payload)
            with self.assertRaises(HTTPException) as caught:
                await startup.list_startup_conversations("owner")
            self.assertEqual(caught.exception.status_code, 503)
            self.assertNotIn("secret", caught.exception.detail)
        self.assertEqual(len(self.requests), 3)

    async def test_missing_or_malformed_bearer_is_rejected_before_upstream(self):
        for authorization in (None, "", "Basic synthetic-token", "Bearer "):
            with self.assertRaises(HTTPException) as caught:
                await startup.get_startup_user(authorization)
            self.assertEqual(caught.exception.status_code, 401)
        self.assertEqual(self.requests, [])

    async def test_admission_limit_after_validation_prevents_database_work(self):
        with patch("app.request_limits.admit_authenticated_user", side_effect=HTTPException(
            429, "Request limit reached. Please try again later.",
        )):
            with self.assertRaises(HTTPException) as caught:
                await startup.get_startup_user("Bearer synthetic-token")
        self.assertEqual(caught.exception.status_code, 429)
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(self.requests[0].url.path, "/auth/v1/user")

    async def test_missing_configuration_and_expired_deadline_do_no_upstream_work(self):
        with patch.object(startup, "_configuration", return_value=(None, None)):
            with self.assertRaises(HTTPException) as caught:
                await startup.get_startup_user("Bearer synthetic-token")
        self.assertEqual(caught.exception.status_code, 503)
        with patch("app.request_limits.remaining_startup_seconds", return_value=0):
            with self.assertRaises(HTTPException) as caught:
                await startup.get_startup_user("Bearer synthetic-token")
        self.assertEqual(caught.exception.status_code, 504)
        self.assertEqual(self.requests, [])

    async def test_shutdown_closes_owned_pool_and_is_idempotent(self):
        await startup.close_startup_client()
        self.assertTrue(self.http.is_closed)
        self.assertIsNone(startup._client)
        await startup.close_startup_client()

    async def test_lifespan_closes_native_pool_even_after_provider_cleanup_failure(self):
        from app import main
        from app.providers import clients

        with patch.object(main, "get_settings", return_value=SimpleNamespace(
            performance_logging_enabled=False,
        )), patch.object(clients, "close_provider_clients", side_effect=RuntimeError(
            "synthetic cleanup failure",
        )), patch.object(main, "close_supabase_client"):
            with self.assertRaisesRegex(RuntimeError, "synthetic cleanup failure"):
                async with main.lifespan(main.app):
                    self.assertEqual((await startup.get_startup_user("Bearer synthetic-token")).id, "owner")
        self.assertTrue(self.http.is_closed)
        self.assertIsNone(startup._client)

    async def test_logging_cleanup_still_runs_if_startup_pool_cleanup_fails(self):
        from app import main
        from app.providers import clients

        close_logging = Mock(return_value=True)
        close_startup = AsyncMock(side_effect=RuntimeError("synthetic native cleanup failure"))
        with patch.object(main, "get_settings", return_value=SimpleNamespace(
            performance_logging_enabled=False,
        )), patch.object(clients, "close_provider_clients"), patch.object(
            main, "close_supabase_client",
        ), patch.object(main, "close_startup_client", close_startup), patch.object(
            main, "shutdown_performance_logging", close_logging,
        ):
            with self.assertRaisesRegex(RuntimeError, "synthetic native cleanup failure"):
                async with main.lifespan(main.app):
                    pass
        close_startup.assert_awaited_once_with()
        close_logging.assert_called_once_with()


class AuthenticationOutageTests(unittest.TestCase):
    def test_sync_sdk_auth_only_definitive_invalid_tokens_are_401(self):
        from supabase_auth.errors import AuthApiError, AuthRetryableError

        for error, expected in (
            (AuthApiError("sensitive", 401, None), 401),
            (AuthApiError("sensitive", 403, "bad_jwt"), 401),
            (AuthApiError("sensitive", 403, None), 503),
            (AuthApiError("sensitive", 500, "bad_jwt"), 503),
            (AuthRetryableError("sensitive", 503), 503),
            (TimeoutError("sensitive"), 503),
            (httpx.ConnectError("sensitive"), 503),
        ):
            with self.subTest(error=type(error).__name__, expected=expected):
                client = Mock()
                client.auth.get_user.side_effect = error
                with patch.object(auth_service, "require_supabase", return_value=client):
                    with self.assertRaises(HTTPException) as caught:
                        auth_service.get_current_user("Bearer synthetic-token")
                self.assertEqual(caught.exception.status_code, expected)
                self.assertNotIn("sensitive", caught.exception.detail)

    def test_sync_missing_config_and_malformed_success_are_503(self):
        with patch.object(auth_service, "require_supabase", side_effect=RuntimeError("sensitive")):
            with self.assertRaises(HTTPException) as caught:
                auth_service.get_current_user("Bearer synthetic-token")
        self.assertEqual(caught.exception.status_code, 503)
        for user in (None, SimpleNamespace()):
            client = Mock()
            client.auth.get_user.return_value = SimpleNamespace(user=user)
            with patch.object(auth_service, "require_supabase", return_value=client):
                with self.assertRaises(HTTPException) as caught:
                    auth_service.get_current_user("Bearer synthetic-token")
            self.assertEqual(caught.exception.status_code, 503)
