"""Bound bodies before FastAPI/Starlette allocate multipart uploads.

The ASGI server supplies chunks; Content-Length is only an additional consistency
check.  A bounded spool is replayed only after the complete body passes validation.
No credentials, filenames, body content, or parser exception text appear in errors.
"""

import asyncio
import json
import logging
from dataclasses import dataclass
from functools import partial
from tempfile import SpooledTemporaryFile

from python_multipart.exceptions import MultipartParseError
from python_multipart.multipart import MultipartParser, parse_options_header


CHAT_BODY_BYTES = 4 * 1024 * 1024
UPLOAD_BODY_BYTES = 21 * 1024 * 1024
API_BODY_BYTES = 16 * 1024
HEALTH_BODY_BYTES = 1024
IO_CHUNK_BYTES = 64 * 1024
_PARSER_LOGGER = logging.Logger("philomath.ingress.multipart", level=logging.CRITICAL + 1)


@dataclass(frozen=True)
class IngressLimits:
    chat_body_bytes: int = CHAT_BODY_BYTES
    upload_body_bytes: int = UPLOAD_BODY_BYTES
    api_body_bytes: int = API_BODY_BYTES
    health_body_bytes: int = HEALTH_BODY_BYTES
    files: int = 5
    file_bytes: int = 10 * 1024 * 1024
    aggregate_file_bytes: int = 20 * 1024 * 1024
    fields: int = 10
    field_bytes: int = 4096
    part_header_bytes: int = 8192
    spool_memory_bytes: int = 1024 * 1024


class _Rejected(Exception):
    def __init__(self, status: int, detail: str):
        self.status = status
        self.detail = detail


def _too_large() -> None:
    raise _Rejected(413, "Request exceeds the allowed resource limits.")


def _malformed() -> None:
    raise _Rejected(422, "Invalid request body.")


class _MultipartBudget:
    """Count streamed parts without retaining uploaded file data."""

    def __init__(self, content_type: bytes, limits: IngressLimits):
        self.limits = limits
        self.file_count = self.field_count = self.file_total = 0
        self.finished = False
        self.chunk_offset = 0
        try:
            media, options = parse_options_header(content_type)
            boundary = options.get(b"boundary", b"")
            if media.lower() != b"multipart/form-data" or not boundary or len(boundary) > 256:
                _malformed()
            self.parser = MultipartParser(
                boundary,
                {
                    "on_part_begin": self._part_begin,
                    "on_header_field": self._header_field,
                    "on_header_value": self._header_value,
                    "on_header_end": self._header_end,
                    "on_headers_finished": self._headers_finished,
                    "on_part_data": self._part_data,
                    "on_end": self._end,
                },
                max_header_count=16,
                max_header_size=limits.part_header_bytes,
            )
            # Malformed requests receive an explicit sanitized response. Library
            # warnings must not synchronously run arbitrary handlers on the loop.
            # This private logger leaves the application's library logger intact.
            self.parser.logger = _PARSER_LOGGER
        except _Rejected:
            raise
        except Exception:
            _malformed()

    def _part_begin(self):
        self.header_name = bytearray()
        self.header_value = bytearray()
        self.headers = {}
        self.header_bytes = 0
        self.first_header_start = None
        self.last_header_end = None
        self.last_field_end = None
        self.part_bytes = 0
        self.is_file = False

    def _header_field(self, data, start, end):
        if self.first_header_start is None:
            self.first_header_start = self.chunk_offset + start
        self.last_field_end = self.chunk_offset + end
        self._raw_header_budget(self.chunk_offset + end)
        self._header_budget(end - start)
        self.header_name.extend(data[start:end])

    def _header_value(self, data, start, end):
        self._raw_header_budget(self.chunk_offset + end)
        self._header_budget(end - start)
        self.header_value.extend(data[start:end])

    def _header_budget(self, amount):
        self.header_bytes += amount
        if self.header_bytes > self.limits.part_header_bytes:
            _too_large()

    def _raw_header_budget(self, absolute_end):
        if self.first_header_start is not None and absolute_end - self.first_header_start > self.limits.part_header_bytes:
            _too_large()

    def _header_end(self):
        # The callback occurs at the current chunk's CR. Count separators and
        # leading whitespace too; callbacks intentionally omit that whitespace.
        cr = self.chunk.find(b"\r", max(0, self.last_field_end - self.chunk_offset))
        if cr < 0:
            _malformed()
        self.last_header_end = self.chunk_offset + cr + 2
        self._raw_header_budget(self.last_header_end)
        name = bytes(self.header_name).lower()
        if name in self.headers:
            _malformed()
        self.headers[name] = bytes(self.header_value)
        self.header_name.clear()
        self.header_value.clear()

    def _headers_finished(self):
        if self.last_header_end is not None:
            self._raw_header_budget(self.last_header_end + 2)
        disposition, options = parse_options_header(self.headers.get(b"content-disposition", b""))
        if disposition.lower() != b"form-data" or b"name" not in options:
            _malformed()
        self.is_file = b"filename" in options
        if self.is_file:
            self.file_count += 1
            if self.file_count > self.limits.files:
                _too_large()
        else:
            self.field_count += 1
            if self.field_count > self.limits.fields:
                _too_large()

    def _part_data(self, data, start, end):
        amount = end - start
        self.part_bytes += amount
        if self.is_file:
            self.file_total += amount
            if self.part_bytes > self.limits.file_bytes or self.file_total > self.limits.aggregate_file_bytes:
                _too_large()
        elif self.part_bytes > self.limits.field_bytes:
            _too_large()

    def _end(self):
        self.finished = True

    def write(self, chunk: bytes):
        self.chunk = chunk
        try:
            self.parser.write(chunk)
        except _Rejected:
            raise
        except MultipartParseError as error:
            if str(error) in {"Maximum header size exceeded", "Maximum header count exceeded"}:
                _too_large()
            _malformed()
        except Exception:
            _malformed()
        finally:
            self.chunk_offset += len(chunk)

    def finish(self):
        # MultipartParser.finalize currently does not validate truncation.
        if not self.finished:
            _malformed()


async def _spool_io(operation, *args):
    """Keep regular-file I/O off-loop, and finish an in-flight call before close.

    Shielding plus awaiting completion on cancellation prevents the spool from
    being closed concurrently with its worker. Each read/write is at most 64 KiB.
    """
    work = asyncio.create_task(asyncio.to_thread(partial(operation, *args)))
    cancelled = None
    while not work.done():
        try:
            await asyncio.shield(work)
        except asyncio.CancelledError as error:
            # Cancellation can be repeated by a deadline, disconnect and server
            # shutdown. Never await the I/O task unshielded, even during cleanup.
            cancelled = error
        except BaseException:
            break
    if cancelled is not None:
        # Retrieve worker failures without letting them replace cancellation.
        try:
            work.result()
        except BaseException:
            pass
        raise cancelled
    return work.result()


class IngressMiddleware:
    def __init__(
        self, app, *, body_timeout: float = 15.0,
        upload_body_timeout: float = 180.0, limits: IngressLimits | None = None,
    ):
        self.app = app
        self.body_timeout = body_timeout
        self.upload_body_timeout = upload_body_timeout
        self.limits = limits or IngressLimits()

    def _limit(self, path: str, method: str) -> int:
        path = path.rstrip("/") or "/"
        if path == "/api/files/upload" and method == "POST":
            return self.limits.upload_body_bytes
        if path == "/api/chat/send" and method == "POST":
            return self.limits.chat_body_bytes
        if path == "/":
            return self.limits.health_body_bytes
        return self.limits.api_body_bytes

    @staticmethod
    async def _error(send, status: int, detail: str):
        payload = json.dumps({"detail": detail}, separators=(",", ":")).encode()
        await send({"type": "http.response.start", "status": status, "headers": [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(payload)).encode()),
        ]})
        await send({"type": "http.response.body", "body": payload})

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        spool = None
        try:
            max_bytes = self._limit(scope.get("path", ""), scope.get("method", "GET"))
            headers = scope.get("headers", [])
            lengths = [value for name, value in headers if name.lower() == b"content-length"]
            if len(lengths) > 1:
                _malformed()
            expected = None
            if lengths:
                raw = lengths[0]
                if not raw or len(raw) > 20 or any(c < 48 or c > 57 for c in raw):
                    _malformed()
                expected = int(raw)
                if expected > max_bytes:
                    _too_large()
                if any(name.lower() == b"transfer-encoding" for name, _ in headers):
                    _malformed()
            content_types = [value for name, value in headers if name.lower() == b"content-type"]
            if len(content_types) > 1 or any(len(value) > 8192 for value in content_types):
                _malformed()
            multipart = None
            if scope.get("path", "").rstrip("/") == "/api/files/upload" and scope.get("method") == "POST":
                multipart = _MultipartBudget(content_types[0] if content_types else b"", self.limits)
            spool = SpooledTemporaryFile(max_size=self.limits.spool_memory_bytes, mode="w+b")
            received = 0
            try:
                # Uploads are admitted/authenticated by the outer limiter first.
                # 180 seconds allows the 20 MiB aggregate on a ~1 Mbps uplink;
                # API bodies retain 15 seconds and startup GETs have an outer
                # 10-second end-to-end deadline.
                timeout = self.upload_body_timeout if multipart is not None else self.body_timeout
                async with asyncio.timeout(timeout):
                    while True:
                        message = await receive()
                        if message["type"] == "http.disconnect":
                            return
                        if message["type"] != "http.request":
                            _malformed()
                        body = message.get("body", b"")
                        received += len(body)
                        if received > max_bytes:
                            _too_large()
                        if not body and message.get("more_body", False):
                            # Buffered empty messages must not starve deadline or
                            # disconnect cancellation callbacks on the event loop.
                            await asyncio.sleep(0)
                        # Do not feed arbitrarily large ASGI chunks into the parser.
                        for offset in range(0, len(body), IO_CHUNK_BYTES):
                            chunk = body[offset:offset + IO_CHUNK_BYTES]
                            if multipart:
                                multipart.write(chunk)
                            await _spool_io(spool.write, chunk)
                        if not message.get("more_body", False):
                            break
                    if expected is not None and received != expected:
                        _malformed()
                    if multipart:
                        multipart.finish()
                    await _spool_io(spool.seek, 0)
            except TimeoutError:
                raise _Rejected(504, "Request body deadline exceeded.") from None
            except OSError:
                raise _Rejected(503, "Request body storage is temporarily unavailable.") from None
            replay_complete = False
            replayed = 0

            async def replay():
                nonlocal replay_complete, replayed
                if replay_complete:
                    # Preserve real disconnect notifications for downstream ASGI.
                    return await receive()
                chunk = await _spool_io(spool.read, IO_CHUNK_BYTES)
                replayed += len(chunk)
                more = replayed < received
                if not more:
                    replay_complete = True
                return {"type": "http.request", "body": chunk, "more_body": more}

            await self.app(scope, replay, send)
        except _Rejected as error:
            await self._error(send, error.status, error.detail)
        finally:
            if spool is not None:
                await _spool_io(spool.close)
