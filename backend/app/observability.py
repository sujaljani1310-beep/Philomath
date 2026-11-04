"""Small structured timers. Never log URLs, tokens, bodies, or exception text."""

import json
import logging
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
from queue import Empty, Full, Queue
from threading import Event, Lock, Thread
from time import perf_counter
from uuid import uuid4


logger = logging.getLogger("philomath.performance")
request_id: ContextVar[str | None] = ContextVar("request_id", default=None)
_configuration_lock = Lock()
_worker = None
_logging_enabled = False


class _PerformanceLogWorker:
    """One bounded, best-effort sink; request threads never invoke handlers."""

    def __init__(self, capacity: int):
        self.queue = Queue(maxsize=capacity)
        self.stopping = Event()
        self.stop_deadline = float("inf")
        self.dropped = 0
        self.failures = 0
        self.thread = Thread(
            target=self._run, name="philomath-performance-log", daemon=True,
        )

    def submit(self, entry: dict) -> None:
        if self.stopping.is_set():
            return
        try:
            self.queue.put_nowait(entry)
        except Full:
            # Drop the newest event. Observability must never hold up a request.
            self.dropped += 1

    def _run(self) -> None:
        try:
            while True:
                if self.stopping.is_set() and perf_counter() >= self.stop_deadline:
                    return
                try:
                    entry = self.queue.get(timeout=0.05)
                except Empty:
                    if self.stopping.is_set():
                        return
                    continue
                try:
                    # Formatting, filters and arbitrary logging handlers all run
                    # here, including handlers installed by a process supervisor.
                    logger.info(json.dumps(entry))
                except Exception:
                    self.failures += 1
                finally:
                    self.queue.task_done()
                if self.stopping.is_set() and self.queue.empty():
                    return
        finally:
            # Pending events are best effort on shutdown. Never wait indefinitely
            # for a sink, and never retain an unbounded backlog.
            while True:
                try:
                    self.queue.get_nowait()
                except Empty:
                    break
                else:
                    self.dropped += 1
                    self.queue.task_done()


def configure_performance_logging(enabled: bool, *, queue_capacity: int = 256) -> None:
    """Configure metadata logging without running sinks in request threads.

    A blocked sink is confined to one daemon thread. Reconfiguration does not
    create more workers while a previous worker is still stopping.
    """
    global _worker, _logging_enabled
    # Configure only our own logger; SDK HTTP logs can contain sensitive URLs.
    if not enabled:
        shutdown_performance_logging(timeout=0)
        logger.disabled = True
        return
    if queue_capacity < 1:
        raise ValueError("Performance log queue capacity must be positive")
    with _configuration_lock:
        _logging_enabled = False
        try:
            logger.disabled = False
            logger.setLevel(logging.INFO)
            if not logger.handlers:
                handler = logging.StreamHandler()
                handler.setFormatter(logging.Formatter("%(message)s"))
                logger.addHandler(handler)
            logger.propagate = False
            if _worker is not None and _worker.thread.is_alive():
                _logging_enabled = not _worker.stopping.is_set()
                return
            worker = _PerformanceLogWorker(queue_capacity)
            worker.thread.start()
        except Exception:
            # Configuration or thread failures must not prevent startup.
            return
        _worker = worker
        _logging_enabled = True


def shutdown_performance_logging(timeout: float = 0.1) -> bool:
    """Stop accepting events and drain for at most ``timeout`` seconds.

    Return False when a handler is still blocked; that daemon is retained until
    it exits so repeated lifespans cannot accumulate blocked sink threads.
    No handler flush/close is called because either could block indefinitely.
    """
    global _worker, _logging_enabled
    timeout = max(0.0, timeout)
    with _configuration_lock:
        _logging_enabled = False
        worker = _worker
        if worker is None:
            return True
        worker.stop_deadline = min(worker.stop_deadline, perf_counter() + timeout)
        worker.stopping.set()
    worker.thread.join(timeout)
    with _configuration_lock:
        stopped = not worker.thread.is_alive()
        if stopped and _worker is worker:
            _worker = None
        return stopped


def record_timing(operation: str, started: float, **fields) -> None:
    try:
        worker = _worker
        if not _logging_enabled or worker is None or not logger.isEnabledFor(logging.INFO):
            return
        worker.submit({
            "event": "performance",
            "operation": operation,
            "duration_ms": round((perf_counter() - started) * 1000, 3),
            "request_id": request_id.get(),
            **fields,
        })
    except Exception:
        # Logging failures must not change a service result or trigger fallback.
        pass


@contextmanager
def timed_operation(operation: str):
    started = perf_counter()
    outcome = "ok"
    try:
        yield
    except BaseException:
        outcome = "error"
        raise
    finally:
        try:
            record_timing(operation, started, outcome=outcome)
        except Exception:
            pass


def timed(operation: str):
    """Time synchronous service functions while preserving FastAPI signatures."""
    def decorate(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            with timed_operation(operation):
                return fn(*args, **kwargs)
        return wrapper
    return decorate


class PerformanceMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        token = request_id.set(uuid4().hex)
        started = perf_counter()
        status = 500

        async def send_with_status(message):
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, send_with_status)
        finally:
            # Use the route template, never the raw path/query (user content).
            try:
                route = scope.get("route")
                record_timing(
                    "http.request", started,
                    route=getattr(route, "path", "[unmatched]"),
                    status=status,
                )
            except Exception:
                pass
            finally:
                request_id.reset(token)
