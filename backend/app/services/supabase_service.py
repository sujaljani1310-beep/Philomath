"""Lazily share one Supabase client and its HTTP connection pool per process."""

from __future__ import annotations

import os
from threading import Lock
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from app.observability import timed_operation
from app.settings import get_settings

if TYPE_CHECKING:
    import httpx
    from supabase import Client


# Retain the existing export without importing the SDK or constructing transports
# during application import. Callers should use require_supabase().
supabase: Client | None = None
_http_client: httpx.Client | None = None
_client_lock = Lock()


def _configuration() -> tuple[str | None, str | None]:
    # Settings loads supported local dotenv configuration once. Never log values.
    get_settings()
    return (
        os.getenv("SUPABASE_URL"),
        os.getenv("SUPABASE_SERVICE_ROLE_KEY") or os.getenv("SUPABASE_KEY"),
    )


def _new_http_client(**kwargs) -> httpx.Client:
    """Keep transport construction replaceable for socket-free SDK tests."""
    import httpx

    return httpx.Client(**kwargs)


def _create_supabase_client(url: str, key: str) -> tuple[Client, httpx.Client]:
    # The SDK imports auth, storage, realtime, and PostgREST dependencies. Keep
    # that work off the import/startup path until the first database request.
    import httpx
    from supabase import ClientOptions, create_client

    from app.http_safety import reject_unsafe_redirects, stateless_cookies

    settings = get_settings()
    database_timeout = httpx.Timeout(
        settings.database_timeout_seconds,
        connect=settings.connect_timeout_seconds,
        pool=settings.connect_timeout_seconds,
    )
    auth_timeout = httpx.Timeout(
        settings.auth_timeout_seconds,
        connect=settings.connect_timeout_seconds,
        pool=settings.connect_timeout_seconds,
    )
    auth_path = urlsplit(url).path.rstrip("/") + "/auth/v1/"

    def apply_timeout(request: httpx.Request) -> None:
        # Supabase accepts one shared HTTP client for both auth and PostgREST.
        # With a supplied client, its postgrest_client_timeout option is ignored.
        # HTTPX request hooks run before transport dispatch; override its timeout
        # extension without changing headers, authentication, or retry behavior.
        timeout = (
            auth_timeout
            if request.url.path.startswith(auth_path)
            else database_timeout
        )
        request.extensions["timeout"] = timeout.as_dict()

    http_client = _new_http_client(
        timeout=database_timeout,
        cookies=stateless_cookies(),
        # Preserve TLS verification and environment proxy handling defaults.
        http2=True,
        follow_redirects=True,
        limits=httpx.Limits(max_connections=100, max_keepalive_connections=20),
        event_hooks={
            "request": [apply_timeout],
            "response": [reject_unsafe_redirects],
        },
    )
    try:
        client = create_client(
            url,
            key,
            options=ClientOptions(
                httpx_client=http_client,
                postgrest_client_timeout=database_timeout,
            ),
        )
        # PostgREST initialization is otherwise lazy and not protected by the SDK
        # against simultaneous first requests. This does not make a network call.
        client.postgrest
        return client, http_client
    except BaseException:
        http_client.close()
        raise


def is_supabase_connected() -> bool:
    """Report configuration presence, retaining the existing no-probe behavior."""
    if supabase is not None:
        return True
    url, key = _configuration()
    return bool(url and key)


def require_supabase() -> Client:
    """Return the process client, initializing it once without a network probe."""
    global supabase, _http_client

    with _client_lock:
        if supabase is None:
            url, key = _configuration()
            if not url or not key:
                raise RuntimeError(
                    "Supabase is not configured. Add SUPABASE_URL and "
                    "SUPABASE_SERVICE_ROLE_KEY to the backend environment."
                )
            with timed_operation("supabase.initialize"):
                supabase, _http_client = _create_supabase_client(url, key)
        return supabase


def close_supabase_client() -> None:
    """Release the owned auth/database pool after application requests drain."""
    global supabase, _http_client

    with _client_lock:
        http_client = _http_client
        supabase = None
        _http_client = None
        if http_client is not None:
            # Every SDK HTTP component uses this shared pool; closing it once
            # releases their connections without constructing unused components.
            http_client.close()
