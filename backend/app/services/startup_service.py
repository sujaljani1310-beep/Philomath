"""Cancellable, deadline-aware reads for the two frontend startup requests.

Only these idempotent reads bypass the synchronous SDK. Authentication still
contacts Supabase for every request; the database credential never substitutes
for validation of the caller's bearer token. No request is retried here.
"""

from __future__ import annotations

import asyncio
import json
import zlib
from typing import Any

import httpx
from fastapi import Header, HTTPException

from app.http_safety import reject_unsafe_redirects, stateless_cookies
from app.observability import timed_operation
from app.services.auth_service import (
    AuthenticatedUser,
    _extract_bearer_token,
    authentication_unavailable,
    invalid_auth_response,
    invalid_session,
)
from app.services.supabase_service import _configuration


STARTUP_PHASE_SECONDS = 4.0
AUTH_RESPONSE_BYTES = 256 * 1024
DATABASE_RESPONSE_BYTES = 8 * 1024 * 1024
_client: httpx.AsyncClient | None = None


async def _redirect_guard(response: httpx.Response) -> None:
    reject_unsafe_redirects(response)


def _new_http_client() -> httpx.AsyncClient:
    """Application-owned pool; native HTTPX retains proxy/NO_PROXY behavior."""
    return httpx.AsyncClient(
        cookies=stateless_cookies(),
        timeout=httpx.Timeout(STARTUP_PHASE_SECONDS, connect=2.0, pool=0.5),
        limits=httpx.Limits(max_connections=64, max_keepalive_connections=16),
        http2=True,
        # Redirects are followed explicitly below, closing their bodies rather
        # than letting HTTPX buffer an unbounded intermediate redirect body.
        follow_redirects=False,
        event_hooks={"response": [_redirect_guard]},
    )


def _get_client() -> httpx.AsyncClient:
    global _client
    # Assignment contains no await. Request tasks on the application loop cannot
    # race initialization, and each request supplies its own immutable headers.
    if _client is None:
        _client = _new_http_client()
    return _client


async def close_startup_client() -> None:
    global _client
    owned, _client = _client, None
    if owned is not None:
        await owned.aclose()


def _deadline_expired() -> bool:
    from app.request_limits import remaining_startup_seconds

    remaining = remaining_startup_seconds()
    return remaining is not None and remaining <= 0


def _phase_budget() -> float:
    from app.request_limits import remaining_startup_seconds

    remaining = remaining_startup_seconds()
    if remaining is None:
        return STARTUP_PHASE_SECONDS
    if remaining <= 0:
        raise HTTPException(504, "Request deadline exceeded. Please try again.")
    return min(STARTUP_PHASE_SECONDS, remaining)


def _database_unavailable() -> HTTPException:
    return HTTPException(503, "Database service is temporarily unavailable. Please try again.")


async def _bounded_response_content(response: httpx.Response, limit: int) -> bytes | bytearray:
    """Cap compressed input and expansion before allocating decoded content.

    HTTPX's normal aiter_bytes() decompresses a complete transport chunk before
    the caller can enforce a limit. Read raw chunks instead and ask zlib for at
    most the remaining budget plus one byte. Never use its unbounded flush().
    """
    encoding = response.headers.get("content-encoding", "identity").strip().lower()
    if encoding not in {"identity", "gzip", "deflate"}:
        raise ValueError("Unsupported upstream content encoding")
    if response.is_stream_consumed:
        # Preloaded synthetic/custom transport responses are already decoded.
        # The application's native response hook never consumes response bodies.
        content = response.content
        if len(content) > limit:
            raise ValueError("Upstream response exceeds the safe response limit")
        return content

    encoded_bytes = 0
    content = bytearray()
    decoder = (
        zlib.decompressobj(zlib.MAX_WBITS | 16) if encoding == "gzip"
        else zlib.decompressobj() if encoding == "deflate" else None
    )
    first_deflate_attempt = encoding == "deflate"
    async for chunk in response.aiter_raw(chunk_size=64 * 1024):
        encoded_bytes += len(chunk)
        if encoded_bytes > limit + 64 * 1024:
            raise ValueError("Upstream encoded response exceeds the safe response limit")
        remaining = limit - len(content)
        try:
            decoded = chunk if decoder is None else decoder.decompress(chunk, remaining + 1)
        except zlib.error:
            if not first_deflate_attempt:
                raise
            # Match HTTPX: try the zlib wrapper first, then retry only the
            # first failed decode as raw deflate using the same bounded input.
            # A valid raw stream may coincidentally begin with a zlib header.
            decoder = zlib.decompressobj(-zlib.MAX_WBITS)
            decoded = decoder.decompress(chunk, remaining + 1)
        first_deflate_attempt = False
        if len(decoded) > remaining:
            raise ValueError("Upstream response exceeds the safe response limit")
        if decoder is not None and decoder.unused_data:
            # One HTTP representation must decode to one complete JSON value.
            # Never accept the first gzip member while silently discarding a
            # second member or arbitrary bytes after the compressed stream.
            raise ValueError("Trailing upstream compressed content is unsupported")
        content.extend(decoded)
    if encoding != "identity" and (decoder is None or not decoder.eof):
        raise ValueError("Incomplete upstream compressed response")
    return content


async def _read_json(
    path: str,
    *,
    user_token: str | None = None,
    params: dict[str, str] | None = None,
) -> tuple[int, Any]:
    """Bound pooling, redirects, streaming, and decoding within one phase.

    No SDK backoff or Retry-After delay is hidden inside the total request
    budget. The ASGI deadline/cancellation scope also covers the complete
    authentication -> database -> serialization sequence.
    """
    authentication = user_token is not None
    unavailable = authentication_unavailable if authentication else _database_unavailable
    try:
        url, key = _configuration()
        if not url or not key:
            raise unavailable()
        headers = {
            "apikey": key,
            "Authorization": f"Bearer {user_token if authentication else key}",
            "Accept": "application/json",
            "Accept-Encoding": "gzip, deflate",
        }
        if not authentication:
            headers["Accept-Profile"] = "public"
        limit = AUTH_RESPONSE_BYTES if authentication else DATABASE_RESPONSE_BYTES
        with timed_operation("auth.get_user" if authentication else "db.startup.list"):
            async with asyncio.timeout(_phase_budget()):
                target = url.rstrip("/") + path
                for redirect in range(21):
                    async with _get_client().stream(
                        "GET", target, headers=headers, params=params,
                    ) as response:
                        if response.has_redirect_location:
                            # The response hook applies the same-origin HTTPS
                            # policy before this location can be used.
                            reject_unsafe_redirects(response)
                            if redirect == 20:
                                raise unavailable()
                            target = str(response.request.url.join(response.headers["location"]))
                            params = None
                            continue
                        content = await _bounded_response_content(response, limit)
                        try:
                            payload = json.loads(content)
                        except (ValueError, UnicodeError):
                            # Only explicit auth status codes, rather than an
                            # untrusted body or exception text, identify 401.
                            if authentication and response.status_code == 401:
                                raise invalid_session() from None
                            raise unavailable() from None
                        return response.status_code, payload
    except HTTPException:
        raise
    except TimeoutError:
        if _deadline_expired():
            raise HTTPException(504, "Request deadline exceeded. Please try again.") from None
        raise unavailable() from None
    except Exception:
        # CancelledError is a BaseException: cancellation closes the streaming
        # response and propagates to transport I/O without a detached worker.
        raise unavailable() from None


async def get_startup_user(
    authorization: str | None = Header(default=None),
) -> AuthenticatedUser:
    from app.request_limits import get_prevalidated_upload_user

    prevalidated = get_prevalidated_upload_user()
    if prevalidated is not None:
        return prevalidated
    token = _extract_bearer_token(authorization)
    status_code, payload = await _read_json("/auth/v1/user", user_token=token)
    code = payload.get("code") if isinstance(payload, dict) else None
    if invalid_auth_response(status_code, code):
        raise invalid_session()
    if status_code != 200 or not isinstance(payload, dict):
        raise authentication_unavailable()
    user_id = payload.get("id")
    email = payload.get("email")
    if not isinstance(user_id, str) or not user_id or (
        email is not None and not isinstance(email, str)
    ):
        raise authentication_unavailable()
    from app.request_limits import admit_authenticated_user

    admit_authenticated_user(user_id)
    return AuthenticatedUser(id=user_id, email=email)


async def _list_rows(path: str, user_id: str, select: str, order: str) -> list[dict]:
    status_code, rows = await _read_json(path, params={
        "select": select, "user_id": f"eq.{user_id}", "order": order,
    })
    if status_code != 200 or not isinstance(rows, list) or any(
        not isinstance(row, dict) for row in rows
    ):
        raise _database_unavailable()
    return rows


async def list_startup_conversations(user_id: str) -> list[dict]:
    return await _list_rows(
        "/rest/v1/conversations", user_id, "*", "updated_at.desc",
    )


async def list_startup_integrations(user_id: str) -> list[dict]:
    return await _list_rows(
        "/rest/v1/user_ai_integrations", user_id,
        "id, provider, display_name, api_key_last4, created_at, updated_at",
        "created_at.asc",
    )
