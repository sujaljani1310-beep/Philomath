"""Provider connection pools without globally retaining users' API keys.

Shared transports own stateless, proxy-aware HTTPX pools. Each call gets its
own SDK and HTTPX wrapper, keeping authorization and cookies request-local.
The application lifespan owns the pools and the unused Gemini async wrapper.
"""

import os
from contextlib import contextmanager
from threading import Lock
from typing import Iterator, Optional

import httpx

from app.http_safety import reject_unsafe_redirects, stateless_cookies
from app.providers.response_limits import bounded_provider_response
from app.settings import get_settings


_pool_lock = Lock()
_pools: dict[str, httpx.BaseTransport] = {}
_MAX_POOLED_ORIGINS = 16
_unused_async_client: httpx.AsyncClient | None = None


def resolve_api_key(
    api_key: Optional[str], default_api_key: Optional[str], variable_name: str
) -> Optional[str]:
    # Settings loads the existing dotenv configuration once, on first use.
    get_settings()
    return api_key or default_api_key or os.getenv(variable_name)


class _BorrowedTransport(httpx.BaseTransport):
    """Use a shared pool without transferring its ownership to an SDK."""

    def __init__(self, connect_timeout: float):
        self._connect_timeout = connect_timeout
        self._local_transports: dict[str, httpx.BaseTransport] = {}

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        # Bound decompression for every negotiated encoding, even when an SDK
        # overwrites the HTTP client's default Accept-Encoding header.
        request.headers["Accept-Encoding"] = "gzip, deflate"
        # Gemini supplies a scalar request timeout, replacing HTTPX's connect
        # timeout. Keep connection acquisition bounded independently of reads.
        timeouts = dict(request.extensions.get("timeout", {}))
        for operation in ("connect", "pool"):
            current = timeouts.get(operation)
            timeouts[operation] = (
                self._connect_timeout
                if current is None
                else min(current, self._connect_timeout)
            )
        request.extensions["timeout"] = timeouts
        # The SDK can choose a configured gateway. Native HTTPX clients own
        # proxy routing, including URL-qualified NO_PROXY and bare proxy URLs.
        origin = str(httpx.URL(
            scheme=request.url.scheme, host=request.url.host, port=request.url.port
        ))
        transport = _get_transport(origin)
        if transport is None:
            # Do not evict/close pools that concurrent requests might be using.
            # Rare overflow origins use an isolated pool for this SDK call.
            transport = self._local_transports.get(origin)
            if transport is None:
                transport = _new_transport(origin)
                self._local_transports[origin] = transport
        return bounded_provider_response(transport.handle_request(request), request)

    def close(self) -> None:
        # Shared pools belong to the application; only overflow pools belong
        # to this request wrapper. Closing twice is harmless.
        transports = list(self._local_transports.values())
        self._local_transports.clear()
        _close_transports(transports)


class _NativeHTTPTransport(httpx.BaseTransport):
    """Share HTTPX's native proxy-aware pools, without session state.

    ``send`` accepts the complete outer request as-is. It does not merge this
    client's headers or cookies into it. Rejecting cookies also prevents the
    shared client from retaining state received from a provider or proxy.
    HTTPX clients are documented as safe to share between threads.
    """

    def __init__(self):
        self._client = httpx.Client(
            cookies=stateless_cookies(),
            follow_redirects=False,
            # Even with redirects disabled, HTTPX prepares ``next_request``.
            # Validate before parsing/preparing a credentialed redirect there.
            event_hooks={"response": [reject_unsafe_redirects]},
            limits=httpx.Limits(
                max_connections=50,
                max_keepalive_connections=20,
                keepalive_expiry=30,
            ),
        )

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        return self._client.send(
            request, stream=True, auth=None, follow_redirects=False
        )

    def close(self) -> None:
        self._client.close()


def _new_transport(origin: str) -> httpx.BaseTransport:
    # Retain an origin-keyed bound on shared pools. The native client dispatches
    # the actual destination using HTTPX's own public environment support.
    return _NativeHTTPTransport()


def _get_transport(origin: str) -> Optional[httpx.BaseTransport]:
    with _pool_lock:
        transport = _pools.get(origin)
        if transport is None and len(_pools) < _MAX_POOLED_ORIGINS:
            transport = _new_transport(origin)
            _pools[origin] = transport
        return transport


@contextmanager
def provider_http_client(base_url: str) -> Iterator[httpx.Client]:
    settings = get_settings()
    with httpx.Client(
        base_url=base_url,
        transport=_BorrowedTransport(settings.provider_connect_timeout_seconds),
        timeout=httpx.Timeout(
            settings.provider_timeout_seconds,
            connect=settings.provider_connect_timeout_seconds,
            pool=settings.provider_connect_timeout_seconds,
        ),
        follow_redirects=True,
        event_hooks={"response": [reject_unsafe_redirects]},
    ) as client:
        yield client


@contextmanager
def openai_client(base_url: str, api_key: str):
    # Importing the SDK is deferred until a provider is actually called.
    from openai import OpenAI

    settings = get_settings()
    with provider_http_client(base_url) as http_client:
        with OpenAI(
            base_url=base_url,
            api_key=api_key,
            http_client=http_client,
            timeout=http_client.timeout,
            # Generation POSTs have no provider-independent idempotency key.
            # A timeout may follow a successful generation: never replay it.
            max_retries=0,
        ) as client:
            yield client


def close_provider_clients() -> None:
    """Release shared pools at shutdown, after in-flight requests finish."""
    with _pool_lock:
        transports = list(_pools.values())
        _pools.clear()
    _close_transports(transports)


def _close_transports(transports: list[httpx.BaseTransport]) -> None:
    """Attempt every cleanup even when one transport's close fails."""
    first_error = None
    for transport in transports:
        try:
            transport.close()
        except Exception as exc:
            if first_error is None:
                first_error = exc
    if first_error is not None:
        raise first_error


class _UnusedAsyncTransport(httpx.AsyncBaseTransport):
    async def handle_async_request(self, request):
        raise NotImplementedError(
            "The synchronous Gemini integration cannot send async requests"
        )


def gemini_unused_async_client() -> httpx.AsyncClient:
    """Supply the SDK's required async slot without creating per-call pools.

    Gemini eagerly creates an async client even for sync-only integrations.
    HttpOptions explicitly supports an application-owned async client. This
    unused wrapper creates no sockets and is closed by application lifespan.
    """
    global _unused_async_client
    with _pool_lock:
        if _unused_async_client is None:
            _unused_async_client = httpx.AsyncClient(
                transport=_UnusedAsyncTransport(),
                cookies=stateless_cookies(),
                trust_env=False,
            )
        return _unused_async_client


async def close_provider_async_clients() -> None:
    global _unused_async_client
    with _pool_lock:
        client = _unused_async_client
        _unused_async_client = None
    if client is not None:
        await client.aclose()
