"""Fail closed on uncertain persistence; never retry a write or invent an ack."""

from contextlib import contextmanager
from threading import RLock

from fastapi import HTTPException


class PersistenceUnavailable(HTTPException):
    def __init__(self):
        super().__init__(
            status_code=503,
            detail=(
                "The database operation could not be confirmed. "
                "Reload to check the current state before trying again."
            ),
        )


@contextmanager
def database_operation():
    """Do not expose upstream bodies, queries, credentials or exception text."""
    try:
        yield
    except PersistenceUnavailable:
        raise
    except Exception:
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
