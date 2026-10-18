"""Process-local admission and end-to-end budgets, without credential caching.

Permits are held for real work, even when a synchronous request outlives its
caller. Startup reads and document uploads are cancellable; generation and
database writes are never replayed or forcibly abandoned by this middleware.
"""

import asyncio
from collections import OrderedDict
from contextvars import ContextVar
from dataclasses import dataclass, field
from math import ceil
from threading import Lock
from time import monotonic, perf_counter
from weakref import WeakSet

from fastapi import HTTPException
from starlette.responses import JSONResponse

from app import observability


STARTUP_DEADLINE_SECONDS = 10.0
MAX_STARTUP_RESPONSE_BYTES = 8 * 1024 * 1024
ERROR_DELIVERY_SECONDS = 0.1
# Concurrent requests, global rate/minute and burst, user concurrency/rate/burst.
POLICIES = {
    "read": (8, 300, 60, 4, 120, 20),
    "chat": (4, 60, 8, 1, 20, 4),
    "upload": (2, 20, 4, 1, 10, 2),
    "write": (8, 120, 20, 2, 30, 5),
}


def _admission_error(category, reason, status, detail, retry_after=1):
    # Reuse the bounded nonblocking sink. A broken logger cannot change admission.
    # No identity, URL, body, credentials or exception text enters this event.
    try:
        observability.record_timing(
            "admission.reject", perf_counter(),
            category=category if category in POLICIES else "unknown",
            reason=reason, status=status, retry_after_seconds=retry_after,
        )
    except Exception:
        pass
    return HTTPException(status, detail, headers={"Retry-After": str(retry_after)})


@dataclass
class _Bucket:
    tokens: float
    updated: float

    def take(self, now, rate, burst):
        self.tokens = min(burst, self.tokens + max(0, now - self.updated) * rate / 60)
        self.updated = now
        if self.tokens < 1:
            return False
        self.tokens -= 1
        return True

    def retry_after(self, rate):
        # take() has just refreshed the rejected bucket at the current clock.
        # Round up HTTP delta-seconds; do not consume/refund tokens or retry work.
        if rate <= 0:
            return 1  # No known refill time: preserve the existing polling hint.
        # Multiply before subtracting to avoid rounding a one-third token into
        # 2.0000000000000004 seconds and incorrectly advertising three seconds.
        return max(1, ceil((60 - self.tokens * 60) / rate))


@dataclass
class _User:
    seen: float
    active: dict = field(default_factory=dict)
    buckets: dict = field(default_factory=dict)


class AdmissionController:
    """Zero-wait admission. Memory and work are bounded per backend process."""

    def __init__(self, *, policies=None, max_active=24, max_users=4096, clock=monotonic):
        self.policies = dict(POLICIES if policies is None else policies)
        self.max_active = max_active
        self.max_users = max_users
        self.clock = clock
        self.lock = Lock()
        self.active = 0
        self.by_category = {}
        self.buckets = {}
        self.users = OrderedDict()

    def acquire_global(self, category):
        with self.lock:
            cap, rate, burst, *_ = self.policies[category]
            if self.active >= self.max_active or self.by_category.get(category, 0) >= cap:
                # Concurrency completion cannot be predicted: one second is a
                # polling hint, not a promise of capacity becoming available.
                raise _admission_error(category, "global_capacity", 503,
                    "Backend capacity is temporarily unavailable.")
            now = self.clock()
            bucket = self.buckets.setdefault(category, _Bucket(burst, now))
            if not bucket.take(now, rate, burst):
                raise _admission_error(category, "global_rate", 429,
                    "Request rate limit exceeded. Try again shortly.", bucket.retry_after(rate))
            self.active += 1
            self.by_category[category] = self.by_category.get(category, 0) + 1

    def acquire_user(self, user_id, category):
        with self.lock:
            now = self.clock()
            # Bounded housekeeping; never evict an active identity or a bucket
            # before its rate history has expired (all buckets refill in <=60s).
            for _ in range(64):
                if not self.users:
                    break
                first_id = next(iter(self.users))
                first = self.users[first_id]
                if now - first.seen < 600 or any(first.active.values()):
                    break
                self.users.popitem(last=False)
            user = self.users.get(user_id)
            if user is None:
                if len(self.users) >= self.max_users:
                    raise _admission_error(category, "user_registry_capacity", 503,
                        "Backend capacity is temporarily unavailable.")
                user = self.users[user_id] = _User(now)
            self.users.move_to_end(user_id)
            user.seen = now
            _, _, _, cap, rate, burst = self.policies[category]
            if user.active.get(category, 0) >= cap:
                raise _admission_error(category, "user_concurrency", 429,
                    "Too many concurrent requests. Try again shortly.")
            bucket = user.buckets.setdefault(category, _Bucket(burst, now))
            if not bucket.take(now, rate, burst):
                raise _admission_error(category, "user_rate", 429,
                    "Request rate limit exceeded. Try again shortly.", bucket.retry_after(rate))
            user.active[category] = user.active.get(category, 0) + 1

    def release(self, state):
        with self.lock:
            if state.released:
                return
            state.released = True
            self.active -= 1
            self.by_category[state.category] -= 1
            if state.user_id is not None:
                user = self.users[state.user_id]
                user.active[state.category] -= 1


@dataclass
class _RequestState:
    controller: AdmissionController
    category: str
    deadline: float | None
    user_id: str | None = None
    released: bool = False
    prevalidated_upload_user: object | None = None


_request_state: ContextVar[_RequestState | None] = ContextVar("resource_request", default=None)


def remaining_startup_seconds() -> float | None:
    state = _request_state.get()
    return None if state is None or state.deadline is None else max(0, state.deadline - monotonic())


def admit_authenticated_user(user_id: str) -> None:
    """Called only after remote authentication; direct service calls have no lease."""
    state = _request_state.get()
    if state is None:
        return
    if state.user_id is None:
        state.controller.acquire_user(user_id, state.category)
        state.user_id = user_id
    elif state.user_id != user_id:
        raise HTTPException(503, "Request authentication state is unavailable.")


def get_prevalidated_upload_user():
    """Only the same upload may reuse its pre-body remote validation result."""
    state = _request_state.get()
    if state is not None and state.category == "upload" and not state.released:
        return state.prevalidated_upload_user
    return None


def _category(path, method):
    if path == "/api/chat/send":
        return "chat"
    if path == "/api/files/upload":
        return "upload"
    return "read" if method in {"GET", "HEAD", "OPTIONS"} else "write"


_instances = WeakSet()


async def _settle(task):
    """Await ownership cleanup despite repeated caller cancellation."""
    interrupted = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            interrupted |= asyncio.current_task().cancelling() > 0
        except Exception:
            break
    return interrupted


async def drain_admitted_requests():
    """Lifespan cleanup must precede closing transports used by shielded work."""
    for instance in _instances:
        tasks = list(instance.inflight)
        if tasks:
            work = asyncio.ensure_future(asyncio.gather(*tasks, return_exceptions=True))
            await _settle(work)


class RequestLimitsMiddleware:
    def __init__(self, app, *, controller=None, startup_deadline=STARTUP_DEADLINE_SECONDS):
        self.app = app
        self.controller = AdmissionController() if controller is None else controller
        self.startup_deadline = startup_deadline
        self.inflight = set()
        _instances.add(self)

    async def _send_error(self, error, scope, receive, send, *, watcher=None, remaining=None):
        """Keep error transmission bounded too, including after work expires."""
        budget = ERROR_DELIVERY_SECONDS if remaining is None else min(ERROR_DELIVERY_SECONDS, max(0, remaining))
        if budget <= 0:
            raise TimeoutError("Error response delivery deadline exceeded.")
        delivery = asyncio.create_task(JSONResponse(
            {"detail": error.detail}, error.status_code, headers=error.headers,
        )(scope, receive, send))
        self.inflight.add(delivery)
        try:
            watched = {delivery} if watcher is None else {delivery, watcher}
            done, _ = await asyncio.wait(watched, timeout=budget, return_when=asyncio.FIRST_COMPLETED)
            if delivery in done:
                return delivery.result()
            if watcher is not None and watcher in done:
                return
            # Headers may already be emitted; never attempt a second status.
            raise TimeoutError("Error response delivery deadline exceeded.")
        finally:
            try:
                if not delivery.done():
                    delivery.cancel()
                if await _settle(delivery):
                    raise asyncio.CancelledError
            finally:
                try:
                    # It may finish with an error just before caller cancellation
                    # enters cleanup, without ever being awaited by _settle.
                    if delivery.done() and not delivery.cancelled():
                        delivery.exception()
                finally:
                    self.inflight.discard(delivery)

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not scope.get("path", "").startswith("/api/"):
            return await self.app(scope, receive, send)
        path = scope["path"].rstrip("/")
        method = scope.get("method", "GET")
        startup = method == "GET" and path in {"/api/conversations", "/api/integrations"}
        cancellable = startup or path == "/api/files/upload"
        category = _category(path, method)
        state = _RequestState(self.controller, category, monotonic() + self.startup_deadline if startup else None)
        try:
            self.controller.acquire_global(category)
        except HTTPException as error:
            return await self._send_error(error, scope, receive, send)
        token = _request_state.set(state)
        body_ready = asyncio.Event()
        messages = []
        response_bytes = 0
        handed_off = False
        disconnected = False
        disconnected_event = asyncio.Event()
        response_started = False

        async def observed_receive():
            if body_ready.is_set():
                await disconnected_event.wait()
                return {"type": "http.disconnect"}
            message = await receive()
            if message["type"] == "http.disconnect":
                disconnected_event.set()
            if message["type"] == "http.disconnect" or (message["type"] == "http.request" and not message.get("more_body", False)):
                body_ready.set()
            return message

        async def monitor_disconnect():
            # Ingress consumes the original body exactly once and then replays
            # its spool; no concurrent receive is used while ingesting bytes.
            await body_ready.wait()
            if disconnected_event.is_set():
                return
            while True:
                message = await receive()
                if message["type"] == "http.disconnect":
                    disconnected_event.set()
                    return

        async def observed_send(message):
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        async def buffered_send(message):
            nonlocal response_bytes
            response_bytes += len(message.get("body", b""))
            if response_bytes > MAX_STARTUP_RESPONSE_BYTES or len(messages) >= 4096:
                raise HTTPException(503, "Upstream response exceeds the safe response limit.")
            messages.append(message)

        async def admitted_app():
            if category == "upload" and method == "POST":
                from app.services.startup_service import get_startup_user

                values = [value for name, value in scope.get("headers", []) if name.lower() == b"authorization"]
                if len(values) > 1:
                    raise HTTPException(401, "Invalid authorization header.")
                # Reject unauthenticated slow uploads and enforce per-user
                # admission before retaining a large body or parsing multipart.
                user = await get_startup_user(values[0].decode("latin-1") if values else None)
                state.prevalidated_upload_user = user
            await self.app(scope, observed_receive, buffered_send if startup else observed_send)
            if startup:
                if remaining_startup_seconds() <= 0:
                    raise HTTPException(504, "Request deadline exceeded.")
                # Forward within the same owned task: timeout/disconnect must
                # also cancel ASGI backpressure and retain the lease until the
                # pending send has unwound.
                for message in messages:
                    await observed_send(message)

        app_task = asyncio.create_task(admitted_app())
        self.inflight.add(app_task)

        def finish(task):
            self.inflight.discard(task)
            self.controller.release(state)
            if not task.cancelled():
                task.exception()  # Consume abandoned-task errors without logging data.

        watcher = asyncio.create_task(monitor_disconnect()) if cancellable else None
        try:
            if cancellable:
                done, _ = await asyncio.wait(
                    {app_task, watcher},
                    # Reserve error delivery within the total ten seconds.
                    timeout=max(0, remaining_startup_seconds() - min(
                        ERROR_DELIVERY_SECONDS, self.startup_deadline * 0.1,
                    )) if startup else None,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if app_task not in done:
                    disconnected = watcher in done
                    app_task.cancel()
                    if await _settle(app_task):
                        raise asyncio.CancelledError
                    if disconnected:
                        return
                    if response_started:
                        # HTTP status cannot be replaced once headers may have
                        # been emitted. Abort partial delivery instead.
                        raise TimeoutError("Response delivery deadline exceeded.")
                    return await self._send_error(
                        HTTPException(504, "Request deadline exceeded."), scope, receive, send,
                        watcher=watcher, remaining=remaining_startup_seconds(),
                    )
                app_task.result()
            else:
                # Don't cancel synchronous provider/database work on client abort;
                # keep its permit until it really finishes, preventing oversubscription.
                await asyncio.shield(app_task)
        except asyncio.CancelledError:
            if cancellable:
                if not app_task.done():
                    app_task.cancel()
                await _settle(app_task)
            else:
                handed_off = True
                app_task.add_done_callback(finish)
            raise
        except HTTPException as error:
            if not response_started:
                await self._send_error(
                    error, scope, receive, send, watcher=watcher,
                    remaining=remaining_startup_seconds() if startup else None,
                )
            else:
                raise
        finally:
            try:
                if watcher is not None:
                    watcher.cancel()
                    await _settle(watcher)
            finally:
                try:
                    if not handed_off:
                        finish(app_task)
                finally:
                    _request_state.reset(token)
