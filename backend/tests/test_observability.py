import asyncio
import json
import logging
import threading
import unittest
from time import monotonic, perf_counter
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app import observability


class CollectingHandler(logging.Handler):
    def __init__(self):
        super().__init__()
        self.records = []
        self.threads = []
        self.condition = threading.Condition()

    def emit(self, record):
        with self.condition:
            self.records.append(record)
            self.threads.append(threading.get_ident())
            self.condition.notify_all()

    def wait_for(self, count, timeout=2):
        with self.condition:
            return self.condition.wait_for(lambda: len(self.records) >= count, timeout)

    def entries(self):
        with self.condition:
            return [json.loads(record.getMessage()) for record in self.records]


class LoggingTestMixin:
    def setUp(self):
        super().setUp()
        self.assertTrue(observability.shutdown_performance_logging(timeout=2))
        logger = observability.logger
        self.previous_logger = (logger.handlers[:], logger.disabled, logger.level, logger.propagate)
        self.sink = CollectingHandler()
        logger.handlers = [self.sink]
        observability.configure_performance_logging(True)

    def tearDown(self):
        self.assertTrue(observability.shutdown_performance_logging(timeout=2))
        logger = observability.logger
        logger.handlers, logger.disabled, logger.level, logger.propagate = self.previous_logger
        super().tearDown()

    def wait_for_entries(self, count):
        self.assertTrue(self.sink.wait_for(count), "background log worker did not emit expected events")
        return self.sink.entries()


class TimerTests(LoggingTestMixin, unittest.TestCase):
    def test_timers_never_log_arguments_or_exception_text(self):
        @observability.timed("db.example")
        def fail(secret):
            raise ValueError(secret)

        with self.assertRaises(ValueError):
            fail("secret-token-user-content")
        entry = self.wait_for_entries(1)[0]
        self.assertEqual(entry["operation"], "db.example")
        self.assertEqual(entry["outcome"], "error")
        self.assertGreaterEqual(entry["duration_ms"], 0)
        self.assertNotIn("secret-token-user-content", self.sink.records[0].getMessage())
        self.assertNotEqual(self.sink.threads[0], threading.get_ident())

    def test_recording_failure_preserves_original_exception_and_success(self):
        @observability.timed("db.example")
        def run(fail):
            if fail:
                raise ValueError("original")
            return "original result"

        with patch.object(observability, "record_timing", side_effect=RuntimeError("broken logger")):
            self.assertEqual(run(False), "original result")
            with self.assertRaisesRegex(ValueError, "original"):
                run(True)

    def test_submit_and_logger_level_failures_are_fail_open(self):
        with patch.object(observability._worker, "submit", side_effect=RuntimeError("queue failed")):
            observability.record_timing("example", perf_counter())
        with patch.object(observability.logger, "isEnabledFor", side_effect=RuntimeError("logger failed")):
            observability.record_timing("example", perf_counter())
        observability.record_timing("healthy", perf_counter())
        self.assertEqual(self.wait_for_entries(1)[0]["operation"], "healthy")

    def test_serialization_failure_does_not_kill_worker(self):
        worker = observability._worker
        observability.record_timing("invalid", perf_counter(), field=object())
        observability.record_timing("healthy", perf_counter())
        self.assertEqual(self.wait_for_entries(1)[0]["operation"], "healthy")
        self.assertEqual(worker.failures, 1)

    def test_handler_failure_does_not_kill_worker(self):
        class FailingOnceHandler(CollectingHandler):
            def emit(self, record):
                if "invalid" in record.getMessage():
                    raise RuntimeError("sink failure")
                super().emit(record)

        self.sink = FailingOnceHandler()
        observability.logger.handlers = [self.sink]
        worker = observability._worker
        observability.record_timing("invalid", perf_counter())
        observability.record_timing("healthy", perf_counter())
        self.assertEqual(self.wait_for_entries(1)[0]["operation"], "healthy")
        self.assertEqual(worker.failures, 1)

    def test_disabled_logging_does_not_enqueue(self):
        observability.configure_performance_logging(False)
        with patch.object(observability.logger, "info") as info:
            observability.record_timing("disabled", perf_counter())
            self.assertTrue(observability.shutdown_performance_logging(timeout=2))
        info.assert_not_called()

    def test_worker_creation_failure_is_fail_open(self):
        self.assertTrue(observability.shutdown_performance_logging(timeout=2))
        with patch.object(observability.Thread, "start", side_effect=RuntimeError("unavailable")):
            observability.configure_performance_logging(True)
            observability.record_timing("dropped", perf_counter())
        self.assertFalse(observability._logging_enabled)
        observability.configure_performance_logging(True)
        observability.record_timing("healthy", perf_counter())
        self.assertEqual(self.wait_for_entries(1)[0]["operation"], "healthy")

    def test_logger_configuration_failure_is_fail_open(self):
        with patch.object(observability.logger, "setLevel", side_effect=RuntimeError("unavailable")):
            observability.configure_performance_logging(True)
            observability.record_timing("dropped", perf_counter())
        self.assertFalse(observability._logging_enabled)
        observability.configure_performance_logging(True)
        observability.record_timing("healthy", perf_counter())
        self.assertEqual(self.wait_for_entries(1)[0]["operation"], "healthy")

    def test_json_formatting_runs_on_background_worker(self):
        serialized_on = []
        dumps = json.dumps

        def capture_thread(*args, **kwargs):
            serialized_on.append(threading.get_ident())
            return dumps(*args, **kwargs)

        with patch.object(observability.json, "dumps", side_effect=capture_thread):
            observability.record_timing("healthy", perf_counter())
            self.wait_for_entries(1)
        self.assertEqual(len(serialized_on), 1)
        self.assertNotEqual(serialized_on[0], threading.get_ident())


class MiddlewareTests(LoggingTestMixin, unittest.IsolatedAsyncioTestCase):
    async def test_logs_route_template_and_resets_context_without_changing_response(self):
        async def app(scope, receive, send):
            scope["route"] = SimpleNamespace(path="/api/conversations/{conversation_id}")
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"original"})

        scope = {
            "type": "http", "path": "/api/conversations/private-user-id",
            "query_string": b"token=secret-token", "headers": [],
        }
        sent = AsyncMock()
        await observability.PerformanceMiddleware(app)(scope, AsyncMock(), sent)
        self.assertIsNone(observability.request_id.get())
        entry = self.wait_for_entries(1)[0]
        self.assertEqual(entry["route"], "/api/conversations/{conversation_id}")
        self.assertEqual(entry["status"], 200)
        self.assertTrue(entry["request_id"])
        self.assertNotIn("private-user-id", self.sink.records[0].getMessage())
        self.assertNotIn("secret-token", self.sink.records[0].getMessage())
        self.assertEqual(sent.await_args_list[-1].args[0]["body"], b"original")

    async def test_uncaught_error_logs_500_and_clears_context(self):
        async def app(*args):
            raise RuntimeError("do not log this")

        with self.assertRaisesRegex(RuntimeError, "do not log this"):
            await observability.PerformanceMiddleware(app)({"type": "http"}, AsyncMock(), AsyncMock())
        entry = self.wait_for_entries(1)[0]
        self.assertEqual(entry["status"], 500)
        self.assertEqual(entry["route"], "[unmatched]")
        self.assertNotIn("do not log this", self.sink.records[0].getMessage())
        self.assertIsNone(observability.request_id.get())

    async def test_concurrent_request_contexts_remain_isolated(self):
        count = 24
        ids = {}
        all_entered = asyncio.Event()

        async def app(scope, receive, send):
            index = scope["index"]
            ids[index] = observability.request_id.get()
            if len(ids) == count:
                all_entered.set()
            await asyncio.wait_for(all_entered.wait(), timeout=2)
            self.assertEqual(observability.request_id.get(), ids[index])
            observability.record_timing("inside", perf_counter(), index=index)
            await asyncio.to_thread(
                observability.record_timing, "inside.thread", perf_counter(), index=index,
            )
            await send({"type": "http.response.start", "status": 200})

        token = observability.request_id.set("outer-context")
        try:
            middleware = observability.PerformanceMiddleware(app)
            await asyncio.gather(*[
                middleware({"type": "http", "index": index}, AsyncMock(), AsyncMock())
                for index in range(count)
            ])
            self.assertEqual(observability.request_id.get(), "outer-context")
        finally:
            observability.request_id.reset(token)
        entries = self.wait_for_entries(count * 3)
        self.assertEqual(len(set(ids.values())), count)
        for entry in entries:
            if entry["operation"] in {"inside", "inside.thread"}:
                self.assertEqual(entry["request_id"], ids[entry["index"]])
        self.assertEqual(
            {entry["request_id"] for entry in entries if entry["operation"] == "http.request"},
            set(ids.values()),
        )

    async def test_recording_errors_never_change_response_or_leak_context(self):
        async def app(scope, receive, send):
            await send({"type": "http.response.start", "status": 201})
            await send({"type": "http.response.body", "body": b"saved"})

        sent = AsyncMock()
        with patch.object(observability, "record_timing", side_effect=RuntimeError("broken logger")):
            await observability.PerformanceMiddleware(app)({"type": "http"}, AsyncMock(), sent)
        self.assertEqual(sent.await_args_list[-1].args[0]["body"], b"saved")
        self.assertIsNone(observability.request_id.get())

    async def test_recording_errors_do_not_mask_downstream_failure(self):
        async def app(*args):
            raise LookupError("original")

        token = observability.request_id.set("outer-context")
        try:
            with patch.object(observability, "record_timing", side_effect=RuntimeError("broken logger")):
                with self.assertRaisesRegex(LookupError, "original"):
                    await observability.PerformanceMiddleware(app)({"type": "http"}, AsyncMock(), AsyncMock())
            self.assertEqual(observability.request_id.get(), "outer-context")
        finally:
            observability.request_id.reset(token)

    async def test_cancellation_clears_context(self):
        async def app(*args):
            raise asyncio.CancelledError()

        with self.assertRaises(asyncio.CancelledError):
            await observability.PerformanceMiddleware(app)({"type": "http"}, AsyncMock(), AsyncMock())
        self.assertIsNone(observability.request_id.get())
        self.assertEqual(self.wait_for_entries(1)[0]["status"], 500)

    async def test_non_http_scope_is_unchanged(self):
        app = AsyncMock()
        scope = {"type": "websocket"}
        receive, send = AsyncMock(), AsyncMock()
        await observability.PerformanceMiddleware(app)(scope, receive, send)
        app.assert_awaited_once_with(scope, receive, send)
        self.assertEqual(self.sink.records, [])

    async def test_blocked_sink_cannot_hold_up_requests_or_accumulate_workers(self):
        self.assertTrue(observability.shutdown_performance_logging(timeout=2))
        entered, release = threading.Event(), threading.Event()

        class BlockingHandler(logging.Handler):
            def emit(self, record):
                entered.set()
                release.wait()

        observability.logger.handlers = [BlockingHandler()]
        observability.configure_performance_logging(True, queue_capacity=2)
        worker = observability._worker
        # A watchdog makes a regression fail instead of hanging the test suite.
        watchdog = threading.Timer(3, release.set)
        watchdog.start()
        try:
            observability.record_timing("first", perf_counter())
            self.assertTrue(await asyncio.to_thread(entered.wait, 1))
            for _ in range(100):
                observability.record_timing("overflow", perf_counter())
            self.assertEqual(worker.queue.qsize(), 2)
            self.assertGreaterEqual(worker.dropped, 98)

            async def app(scope, receive, send):
                await send({"type": "http.response.start", "status": 200})
                await send({"type": "http.response.body", "body": b"original"})

            sent = AsyncMock()
            await asyncio.wait_for(
                observability.PerformanceMiddleware(app)({"type": "http"}, AsyncMock(), sent),
                timeout=1,
            )
            self.assertEqual(sent.await_args_list[-1].args[0]["body"], b"original")
            self.assertFalse(release.is_set(), "request waited for the blocked sink")
            started = monotonic()
            self.assertFalse(observability.shutdown_performance_logging(timeout=0.01))
            self.assertLess(monotonic() - started, 1)
            for _ in range(10):
                observability.configure_performance_logging(True)
                self.assertIs(observability._worker, worker)
                self.assertFalse(observability._logging_enabled)
            self.assertFalse(release.is_set(), "reconfiguration waited for the blocked sink")
        finally:
            release.set()
            watchdog.cancel()
            self.assertTrue(await asyncio.to_thread(observability.shutdown_performance_logging, 2))
        observability.logger.handlers = [self.sink]
        observability.configure_performance_logging(True)
        self.assertIsNot(observability._worker, worker)
        observability.record_timing("after-restart", perf_counter())
        self.assertEqual(self.wait_for_entries(1)[0]["operation"], "after-restart")
