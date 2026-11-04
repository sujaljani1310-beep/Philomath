import asyncio
import gc
from concurrent.futures import ThreadPoolExecutor
from threading import Event
import unittest
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.ingress import IngressMiddleware
from app import request_limits as limits


def policies(**changes):
    result = {name: (24, 6000, 1000, 8, 6000, 1000) for name in limits.POLICIES}
    result.update(changes)
    return result


class ControllerTests(unittest.TestCase):
    def test_per_user_rate_refill_and_identity_isolation(self):
        clock = [10.0]
        controller = limits.AdmissionController(policies=policies(read=(8, 600, 100, 4, 60, 1)), clock=lambda: clock[0])
        controller.acquire_user("a", "read")
        with self.assertRaises(HTTPException) as caught:
            controller.acquire_user("a", "read")
        self.assertEqual(caught.exception.status_code, 429)
        controller.acquire_user("b", "read")
        clock[0] += 1
        controller.acquire_user("a", "read")

    def test_global_rate_and_concurrency_are_distinct_and_recover(self):
        clock = [10.0]
        controller = limits.AdmissionController(policies=policies(read=(1, 60, 1, 1, 60, 1)), clock=lambda: clock[0])
        controller.acquire_global("read")
        state = limits._RequestState(controller, "read", None)
        with self.assertRaises(HTTPException) as caught:
            controller.acquire_global("read")
        self.assertEqual(caught.exception.status_code, 503)
        controller.release(state)
        controller.release(state)  # Repeated cleanup cannot underflow counters.
        with self.assertRaises(HTTPException) as caught:
            controller.acquire_global("read")
        self.assertEqual(caught.exception.status_code, 429)
        clock[0] += 1
        controller.acquire_global("read")
        self.assertEqual(controller.active, 1)

    def test_user_tracking_is_bounded_and_idle_state_expires(self):
        clock = [10.0]
        controller = limits.AdmissionController(policies=policies(), max_users=2, clock=lambda: clock[0])
        for identity in ("a", "b"):
            controller.acquire_global("read")
            controller.acquire_user(identity, "read")
            controller.release(limits._RequestState(controller, "read", None, user_id=identity))
        with self.assertRaises(HTTPException) as caught:
            controller.acquire_user("c", "read")
        self.assertEqual(caught.exception.status_code, 503)
        self.assertEqual(len(controller.users), 2)
        clock[0] += 601
        controller.acquire_user("c", "read")
        self.assertEqual(list(controller.users), ["c"])

    def test_parallel_admission_cannot_exceed_global_cap(self):
        controller = limits.AdmissionController(policies=policies(), max_active=4)
        def enter(_):
            try:
                controller.acquire_global("read")
                return True
            except HTTPException:
                return False
        with ThreadPoolExecutor(max_workers=12) as pool:
            accepted = list(pool.map(enter, range(100)))
        self.assertEqual(sum(accepted), 4)
        self.assertEqual(controller.active, 4)


async def invoke(middleware, *, path="/api/conversations", method="GET", identity="a", disconnect=None, on_send=None):
    sent = []
    body_sent = False
    async def receive():
        nonlocal body_sent
        if not body_sent:
            body_sent = True
            return {"type": "http.request", "body": b"", "more_body": False}
        if disconnect is not None:
            await disconnect.wait()
            return {"type": "http.disconnect"}
        await asyncio.Future()
    async def send(message):
        sent.append(message)
        if on_send is not None:
            await on_send(message)
    await middleware({"type": "http", "path": path, "method": method, "headers": [], "identity": identity}, receive, send)
    return sent


async def ok(send):
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b"original"})


class MiddlewareTests(unittest.IsolatedAsyncioTestCase):
    def middleware(self, app, **kwargs):
        controller = kwargs.pop("controller", limits.AdmissionController(policies=policies()))
        return limits.RequestLimitsMiddleware(IngressMiddleware(app), controller=controller, **kwargs)

    async def test_total_deadline_cancels_work_and_recovers(self):
        cancelled = asyncio.Event()
        slow = True
        async def app(scope, receive, send):
            limits.admit_authenticated_user(scope["identity"])
            if slow:
                try:
                    await asyncio.Future()
                finally:
                    cancelled.set()
            await ok(send)
        # Leave enough scaled error-delivery reserve for real spool cleanup
        # and scheduler jitter; assertions still require cancellation and 504.
        middleware = self.middleware(app, startup_deadline=0.2)
        response = await asyncio.wait_for(invoke(middleware), 1)
        self.assertEqual(response[0]["status"], 504)
        self.assertTrue(cancelled.is_set())
        self.assertEqual(middleware.controller.active, 0)
        slow = False
        self.assertEqual((await invoke(middleware))[0]["status"], 200)
        self.assertIsNone(limits._request_state.get())

    async def test_serialization_time_is_included_before_committing_response(self):
        async def app(scope, receive, send):
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await asyncio.sleep(0.4)
            await send({"type": "http.response.body", "body": b"original"})
        response = await invoke(self.middleware(app, startup_deadline=0.2))
        self.assertEqual(response[0]["status"], 504)
        self.assertNotIn(b"original", [message.get("body") for message in response])

    async def test_startup_deadline_cancels_blocked_header_and_body_delivery(self):
        async def app(scope, receive, send):
            await ok(send)
        for blocked_type in ("http.response.start", "http.response.body"):
            with self.subTest(blocked_type=blocked_type):
                entered, stopped = asyncio.Event(), asyncio.Event()
                async def slow_send(message):
                    if message["type"] == blocked_type:
                        entered.set()
                        try:
                            await asyncio.Future()
                        finally:
                            stopped.set()
                middleware = self.middleware(app, startup_deadline=0.03)
                task = asyncio.create_task(invoke(middleware, on_send=slow_send))
                await asyncio.wait_for(entered.wait(), 1)
                with self.assertRaisesRegex(TimeoutError, "Response delivery deadline exceeded"):
                    await asyncio.wait_for(task, 1)
                self.assertTrue(stopped.is_set())
                self.assertEqual(middleware.controller.active, 0)
                self.assertFalse(middleware.inflight)
                self.assertIsNone(limits._request_state.get())

    async def test_disconnect_during_response_delivery_releases_owned_work(self):
        entered, stopped, disconnect = asyncio.Event(), asyncio.Event(), asyncio.Event()
        async def app(scope, receive, send):
            await ok(send)
        async def slow_send(message):
            if message["type"] == "http.response.body":
                entered.set()
                try:
                    await asyncio.Future()
                finally:
                    stopped.set()
        middleware = self.middleware(app)
        task = asyncio.create_task(invoke(middleware, disconnect=disconnect, on_send=slow_send))
        await asyncio.wait_for(entered.wait(), 1)
        disconnect.set()
        await asyncio.wait_for(task, 1)
        self.assertTrue(stopped.is_set())
        self.assertEqual(middleware.controller.active, 0)
        self.assertFalse(middleware.inflight)

    async def test_repeated_cancel_during_send_keeps_lease_until_cleanup_finishes(self):
        entered, cleaning, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
        async def app(scope, receive, send):
            await ok(send)
        async def slow_send(message):
            if message["type"] == "http.response.body":
                entered.set()
                try:
                    await asyncio.Future()
                finally:
                    cleaning.set()
                    await release.wait()
        middleware = self.middleware(app)
        task = asyncio.create_task(invoke(middleware, on_send=slow_send))
        await asyncio.wait_for(entered.wait(), 1)
        task.cancel()
        await asyncio.wait_for(cleaning.wait(), 1)
        task.cancel()
        await asyncio.sleep(0)
        self.assertFalse(task.done())
        self.assertEqual(middleware.controller.active, 1)
        release.set()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(middleware.controller.active, 0)
        self.assertFalse(middleware.inflight)

    async def test_generated_504_delivery_is_bounded_for_header_and_body(self):
        async def app(scope, receive, send):
            await asyncio.Future()
        for blocked_type in ("http.response.start", "http.response.body"):
            with self.subTest(blocked_type=blocked_type):
                statuses, stopped = [], asyncio.Event()
                async def slow_send(message):
                    if message["type"] == "http.response.start":
                        statuses.append(message["status"])
                    if message["type"] == blocked_type:
                        try:
                            await asyncio.Future()
                        finally:
                            stopped.set()
                middleware = self.middleware(app, startup_deadline=0.05)
                with self.assertRaisesRegex(TimeoutError, "Error response delivery"):
                    await asyncio.wait_for(invoke(middleware, on_send=slow_send), 1)
                self.assertEqual(statuses, [504])
                self.assertTrue(stopped.is_set())
                self.assertEqual(middleware.controller.active, 0)
                self.assertFalse(middleware.inflight)

    async def test_caught_http_error_delivery_is_bounded(self):
        async def app(scope, receive, send):
            raise HTTPException(503, "Synthetic unavailable.")
        stopped = asyncio.Event()
        async def slow_send(message):
            if message["type"] == "http.response.body":
                try:
                    await asyncio.Future()
                finally:
                    stopped.set()
        middleware = self.middleware(app)
        with self.assertRaisesRegex(TimeoutError, "Error response delivery"):
            await asyncio.wait_for(invoke(middleware, on_send=slow_send), 1)
        self.assertTrue(stopped.is_set())
        self.assertEqual(middleware.controller.active, 0)
        self.assertFalse(middleware.inflight)

    async def test_disconnect_during_generated_error_cancels_delivery(self):
        entered, stopped, disconnect = asyncio.Event(), asyncio.Event(), asyncio.Event()
        async def app(scope, receive, send):
            raise HTTPException(503, "Synthetic unavailable.")
        async def slow_send(message):
            if message["type"] == "http.response.body":
                entered.set()
                try:
                    await asyncio.Future()
                finally:
                    stopped.set()
        middleware = self.middleware(app)
        task = asyncio.create_task(invoke(middleware, disconnect=disconnect, on_send=slow_send))
        await asyncio.wait_for(entered.wait(), 1)
        disconnect.set()
        await asyncio.wait_for(task, 1)
        self.assertTrue(stopped.is_set())
        self.assertEqual(middleware.controller.active, 0)
        self.assertFalse(middleware.inflight)

    async def test_repeated_cancel_during_error_delivery_preserves_cleanup_lease(self):
        entered, cleaning, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
        async def app(scope, receive, send):
            raise HTTPException(503, "Synthetic unavailable.")
        async def slow_send(message):
            if message["type"] == "http.response.body":
                entered.set()
                try:
                    await asyncio.Future()
                finally:
                    cleaning.set()
                    await release.wait()
        middleware = self.middleware(app)
        task = asyncio.create_task(invoke(middleware, on_send=slow_send))
        await asyncio.wait_for(entered.wait(), 1)
        task.cancel()
        await asyncio.wait_for(cleaning.wait(), 1)
        task.cancel()
        await asyncio.sleep(0)
        self.assertFalse(task.done())
        self.assertEqual(middleware.controller.active, 1)
        release.set()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(middleware.controller.active, 0)
        self.assertFalse(middleware.inflight)

    async def test_global_capacity_rejection_cannot_leave_blocked_delivery_task(self):
        controller = limits.AdmissionController(policies=policies(), max_active=0)
        app = AsyncMock()
        async def slow_send(message):
            if message["type"] == "http.response.body":
                await asyncio.Future()
        middleware = self.middleware(app, controller=controller)
        with self.assertRaisesRegex(TimeoutError, "Error response delivery"):
            await asyncio.wait_for(invoke(middleware, on_send=slow_send), 1)
        app.assert_not_awaited()
        self.assertEqual(controller.active, 0)
        self.assertFalse(middleware.inflight)
        self.assertIsNone(limits._request_state.get())

    async def test_error_delivery_completion_racing_cancel_does_not_log_unretrieved_exception(self):
        entered, release = asyncio.Event(), asyncio.Event()
        loop = asyncio.get_running_loop()
        previous = loop.get_exception_handler()
        errors = []
        loop.set_exception_handler(lambda loop, context: errors.append(context))
        async def send(message):
            entered.set()
            await release.wait()
            raise RuntimeError("synthetic private transport text")
        middleware = self.middleware(AsyncMock())
        try:
            work = asyncio.create_task(middleware._send_error(
                HTTPException(503, "Synthetic unavailable."),
                {"type": "http"}, AsyncMock(), send,
            ))
            await asyncio.wait_for(entered.wait(), 1)
            # The delivery finishes before the cancelled parent's cleanup runs.
            release.set()
            work.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await work
            del work
            gc.collect()
            await asyncio.sleep(0)
            self.assertFalse(errors, "Owned delivery errors escaped to unhandled task logging")
            self.assertFalse(middleware.inflight)
        finally:
            release.set()
            loop.set_exception_handler(previous)

    async def test_disconnect_cancels_upstream_and_releases_permits(self):
        entered, cancelled, disconnect = asyncio.Event(), asyncio.Event(), asyncio.Event()
        async def app(scope, receive, send):
            limits.admit_authenticated_user(scope["identity"])
            entered.set()
            try:
                await asyncio.Future()
            finally:
                cancelled.set()
        middleware = self.middleware(app)
        request = asyncio.create_task(invoke(middleware, disconnect=disconnect))
        await entered.wait()
        disconnect.set()
        self.assertEqual(await asyncio.wait_for(request, 1), [])
        self.assertTrue(cancelled.is_set())
        self.assertEqual(middleware.controller.active, 0)

    async def test_user_concurrency_does_not_block_another_identity(self):
        release, entered = asyncio.Event(), asyncio.Event()
        controller = limits.AdmissionController(policies=policies(read=(8, 6000, 100, 1, 6000, 100)))
        async def app(scope, receive, send):
            limits.admit_authenticated_user(scope["identity"])
            if scope["identity"] == "a":
                entered.set()
                await release.wait()
            await ok(send)
        middleware = self.middleware(app, controller=controller)
        first = asyncio.create_task(invoke(middleware))
        await entered.wait()
        try:
            self.assertEqual((await invoke(middleware))[0]["status"], 429)
            self.assertEqual((await invoke(middleware, identity="b"))[0]["status"], 200)
            self.assertEqual(controller.active, 1)
        finally:
            release.set()
            await first
        self.assertEqual(controller.active, 0)

    async def test_cancelled_write_retains_capacity_until_actual_work_finishes(self):
        from starlette.concurrency import run_in_threadpool

        entered, release, finished = Event(), Event(), asyncio.Event()
        controller = limits.AdmissionController(policies=policies(), max_active=1)
        def blocking_work():
            entered.set()
            if not release.wait(3):
                raise RuntimeError("Synthetic worker was not released")
        async def app(scope, receive, send):
            limits.admit_authenticated_user(scope["identity"])
            await run_in_threadpool(blocking_work)
            finished.set()
            await ok(send)
        middleware = self.middleware(app, controller=controller)
        task = asyncio.create_task(invoke(middleware, path="/api/chat/send", method="POST"))
        try:
            self.assertTrue(await asyncio.to_thread(entered.wait, 1))
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertEqual(controller.active, 1)
            self.assertFalse(finished.is_set())
            self.assertEqual((await invoke(middleware))[0]["status"], 503)
        finally:
            release.set()
            await limits.drain_admitted_requests()
        self.assertTrue(finished.is_set())
        self.assertEqual(controller.active, 0)

    async def test_repeated_cancellation_keeps_lease_through_owned_cleanup(self):
        entered, cleaning, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
        async def app(scope, receive, send):
            entered.set()
            try:
                await asyncio.Future()
            finally:
                cleaning.set()
                await release.wait()
        middleware = self.middleware(app)
        task = asyncio.create_task(invoke(middleware))
        await entered.wait()
        task.cancel()
        await cleaning.wait()
        task.cancel()
        await asyncio.sleep(0)
        self.assertFalse(task.done())
        self.assertEqual(middleware.controller.active, 1)
        release.set()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(middleware.controller.active, 0)

    async def test_unhandled_exception_does_not_leak_permits_or_context(self):
        async def app(scope, receive, send):
            limits.admit_authenticated_user("a")
            raise RuntimeError("synthetic failure")
        middleware = self.middleware(app)
        with self.assertRaisesRegex(RuntimeError, "synthetic failure"):
            await invoke(middleware)
        self.assertEqual(middleware.controller.active, 0)
        self.assertIsNone(limits._request_state.get())

    async def test_invalid_upload_auth_is_rejected_before_reading_body(self):
        middleware = self.middleware(AsyncMock())
        receive, send = AsyncMock(), AsyncMock()
        with patch("app.services.startup_service.get_startup_user", new=AsyncMock(
            side_effect=HTTPException(401, "Your login session is invalid or expired.")
        )):
            await middleware({"type": "http", "path": "/api/files/upload", "method": "POST", "headers": []}, receive, send)
        receive.assert_not_awaited()
        self.assertEqual(send.await_args_list[0].args[0]["status"], 401)
        self.assertEqual(middleware.controller.active, 0)

    async def test_upload_user_admission_precedes_body_and_cache_is_request_local(self):
        from app.services.auth_service import AuthenticatedUser
        calls = []
        async def auth(authorization):
            calls.append(authorization)
            limits.admit_authenticated_user("synthetic-upload-owner")
            return AuthenticatedUser("synthetic-upload-owner")
        async def app(scope, receive, send):
            self.assertEqual(limits.get_prevalidated_upload_user().id, "synthetic-upload-owner")
            await ok(send)
        middleware = limits.RequestLimitsMiddleware(app, controller=limits.AdmissionController(policies=policies()))
        for _ in range(2):
            with patch("app.services.startup_service.get_startup_user", new=auth):
                await invoke(middleware, path="/api/files/upload", method="POST")
            self.assertIsNone(limits.get_prevalidated_upload_user())
        self.assertEqual(calls, [None, None])
        self.assertEqual(middleware.controller.active, 0)


class IntegratedErrorTests(unittest.TestCase):
    def test_early_errors_keep_cors_and_validation_does_not_echo_credentials(self):
        from app.main import app
        from app.services.auth_service import AuthenticatedUser, get_current_user
        with patch.dict(app.dependency_overrides, {get_current_user: lambda: AuthenticatedUser("synthetic-limit-user")}):
            with TestClient(app) as client:
                response = client.post("/api/chat/send", content=b"", headers={"content-length": "999999999", "origin": "http://localhost:3000"})
                self.assertEqual(response.status_code, 413)
                self.assertEqual(response.headers["access-control-allow-origin"], "http://localhost:3000")
                response = client.post("/api/integrations", json={"ai_name": "gemini", "api_key": "secret"})
                self.assertEqual(response.status_code, 422)
                self.assertNotIn("secret", response.text)
                self.assertNotIn("input", response.json()["detail"][0])
                response = client.options("/api/files/upload", headers={"origin": "http://localhost:3000", "access-control-request-method": "POST"})
                self.assertEqual(response.status_code, 200)
