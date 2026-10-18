"""Fail closed on uncertain persistence; never retry a write or invent an ack."""

from contextlib import contextmanager
from threading import RLock
from time import perf_counter

from fastapi import HTTPException

from app import observability


class PersistenceUnavailable(HTTPException):
    def __init__(self):
        super().__init__(
            status_code=503,
            detail=(
                "The database operation could not be confirmed. "
                "Reload to check the current state before trying again."
            ),
        )


def _read_failure_category(error):
    # Classify types and a fixed set of SDK codes only. Never stringify errors or
    # emit upstream messages, query details, URLs, response bodies or arbitrary codes.
    if isinstance(error, PersistenceUnavailable):
        return "validation_or_ownership"
    import httpx
    from postgrest.exceptions import APIError

    for kind, category in (
        (httpx.PoolTimeout, "pool_timeout"),
        (httpx.ConnectTimeout, "connect_timeout"),
        (httpx.ReadTimeout, "read_timeout"),
        (httpx.WriteTimeout, "write_timeout"),
        (httpx.TimeoutException, "timeout"),
        (httpx.ProtocolError, "protocol_error"),
        (httpx.NetworkError, "connection_error"),
        (httpx.TransportError, "transport_error"),
    ):
        if isinstance(error, kind):
            return category
    if isinstance(error, APIError):
        categories = {
            "42501": "upstream_permission", "42P01": "upstream_schema",
            "42703": "upstream_schema", "PGRST204": "upstream_schema",
            "PGRST205": "upstream_schema", "57014": "upstream_timeout",
            "PGRST301": "upstream_auth", "PGRST302": "upstream_auth",
            "PGRST303": "upstream_auth",
        }
        return categories.get(error.code, "upstream_api") if isinstance(error.code, str) else "upstream_api"
    if isinstance(error, httpx.HTTPStatusError):
        status = error.response.status_code
        if status == 429:
            return "upstream_rate"
        if status == 401:
            return "upstream_auth"
        if status == 403:
            return "upstream_permission"
        if 500 <= status <= 599:
            return "upstream_unavailable"
        return "upstream_http"
    return "unexpected_error"


def _record_read_failure(operation, started, error):
    try:
        observability.record_timing(
            "db.read.failure", started,
            read_operation="conversation_history" if operation == "conversation_history" else "unknown",
            category=_read_failure_category(error), outcome="error",
        )
    except Exception:
        # Classification/import/sink failure must never affect the original result.
        pass


@contextmanager
def database_operation(*, read_operation=None):
    """Do not expose upstream bodies, queries, credentials or exception text."""
    started = perf_counter() if read_operation is not None else None
    try:
        yield
    except Exception as error:
        if read_operation is not None:
            _record_read_failure(read_operation, started, error)
        if isinstance(error, PersistenceUnavailable):
            raise
        raise PersistenceUnavailable() from None


def affected_rows(result, *, expected=None):
    count = getattr(result, "count", None)
    if type(count) is not int or count < 0 or (expected is not None and count != expected):
        raise PersistenceUnavailable()
    return count


# Fixed memory, no user/key registry. Hash collisions only serialize work.
# These guards coordinate mutations in this process, not across workers/replicas.
_mutation_locks = tuple(RLock() for _ in range(256))
MUTATION_LOCK_TIMEOUT_SECONDS = 1.0


@contextmanager
def mutation_guard(namespace, identity):
    lock = _mutation_locks[hash((namespace, identity)) % len(_mutation_locks)]
    if not lock.acquire(timeout=MUTATION_LOCK_TIMEOUT_SECONDS):
        raise PersistenceUnavailable()
    try:
        yield
    finally:
        lock.release()
