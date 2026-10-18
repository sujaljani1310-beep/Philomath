"""Admission/read diagnostics use only synthetic identities and socket-free errors."""
from concurrent.futures import ThreadPoolExecutor
import json
import logging
from threading import Event
import unittest
from unittest.mock import patch

from fastapi import HTTPException
import httpx
from postgrest.exceptions import APIError

from app import observability
from app import request_limits as limits
from app.services import persistence
from tests.test_observability import LoggingTestMixin


class AdmissionDiagnosticTests(unittest.TestCase):
    def test_all_rejection_reasons_are_fixed_and_do_not_change_status_or_detail(self):
        cases = []
        global_capacity = limits.AdmissionController(max_active=0)
        cases.append((lambda: global_capacity.acquire_global("chat"), "global_capacity", 503,
                      "Backend capacity is temporarily unavailable."))
        global_rate = limits.AdmissionController(clock=lambda: 0)
        for _ in range(8):
            global_rate.acquire_global("chat")
            global_rate.release(limits._RequestState(global_rate, "chat", None))
        cases.append((lambda: global_rate.acquire_global("chat"), "global_rate", 429,
                      "Request rate limit exceeded. Try again shortly."))
        user_capacity = limits.AdmissionController(max_users=0)
        cases.append((lambda: user_capacity.acquire_user("SYNTHETIC_PRIVATE_USER", "chat"),
                      "user_registry_capacity", 503, "Backend capacity is temporarily unavailable."))
        user_concurrency = limits.AdmissionController()
        user_concurrency.acquire_user("SYNTHETIC_PRIVATE_USER", "chat")
        cases.append((lambda: user_concurrency.acquire_user("SYNTHETIC_PRIVATE_USER", "chat"),
                      "user_concurrency", 429, "Too many concurrent requests. Try again shortly."))
        user_rate = limits.AdmissionController(clock=lambda: 0)
        for _ in range(4):
            user_rate.acquire_global("chat")
            user_rate.acquire_user("SYNTHETIC_PRIVATE_USER", "chat")
            user_rate.release(limits._RequestState(user_rate, "chat", None, user_id="SYNTHETIC_PRIVATE_USER"))
        cases.append((lambda: user_rate.acquire_user("SYNTHETIC_PRIVATE_USER", "chat"),
                      "user_rate", 429, "Request rate limit exceeded. Try again shortly."))
        for reject, reason, status, detail in cases:
            with self.subTest(reason=reason), patch.object(observability, "record_timing") as record:
                with self.assertRaises(HTTPException) as caught:
                    reject()
                self.assertEqual(caught.exception.status_code, status)
                self.assertEqual(caught.exception.detail, detail)
                record.assert_called_once()
                self.assertEqual(record.call_args.args[0], "admission.reject")
                fields = record.call_args.kwargs
                self.assertEqual(set(fields), {"category", "reason", "status", "retry_after_seconds"})
                self.assertEqual((fields["reason"], fields["status"], fields["category"]), (reason, status, "chat"))
                self.assertEqual(str(fields["retry_after_seconds"]), caught.exception.headers["Retry-After"])
                self.assertNotIn("SYNTHETIC_PRIVATE", json.dumps(fields))

    def test_fractional_wait_is_rounded_up_without_mutating_tokens(self):
        bucket = limits._Bucket(tokens=0.25, updated=10.0)
        self.assertEqual(bucket.retry_after(20), 3)
        self.assertEqual((bucket.tokens, bucket.updated), (0.25, 10.0))
        for rate, expected in ((10, 5), (20, 3), (30, 2), (60, 1), (120, 1)):
            with self.subTest(rate=rate):
                self.assertEqual(bucket.retry_after(rate), expected)
        self.assertEqual(bucket.retry_after(0), 1)

    def test_actual_custom_global_rate_controls_retry_after(self):
        policy = dict(limits.POLICIES, chat=(4, 10, 1, 1, 20, 4))
        controller = limits.AdmissionController(policies=policy, clock=lambda: 10)
        controller.acquire_global("chat")
        controller.release(limits._RequestState(controller, "chat", None))
        with self.assertRaises(HTTPException) as caught:
            controller.acquire_global("chat")
        self.assertEqual(caught.exception.headers["Retry-After"], "6")

    def test_each_default_category_uses_its_own_global_and_user_refill(self):
        for category, global_wait, user_wait in (("read", "1", "1"), ("chat", "1", "3"),
                                                 ("upload", "3", "6"), ("write", "1", "2")):
            with self.subTest(category=category):
                controller = limits.AdmissionController(clock=lambda: 0)
                for _ in range(controller.policies[category][2]):
                    controller.acquire_global(category)
                    controller.release(limits._RequestState(controller, category, None))
                with self.assertRaises(HTTPException) as caught:
                    controller.acquire_global(category)
                self.assertEqual(caught.exception.headers["Retry-After"], global_wait)
                controller = limits.AdmissionController(clock=lambda: 0)
                for _ in range(controller.policies[category][5]):
                    controller.acquire_global(category)
                    controller.acquire_user("synthetic", category)
                    controller.release(limits._RequestState(controller, category, None, user_id="synthetic"))
                with self.assertRaises(HTTPException) as caught:
                    controller.acquire_user("synthetic", category)
                self.assertEqual(caught.exception.headers["Retry-After"], user_wait)

    def test_advertised_rounded_wait_allows_refill_after_fractional_elapsed_time(self):
        for rate in (10, 20, 30, 60, 120, 300):
            for fraction in (0, 0.1, 0.25, 1 / 3, 0.5, 0.9):
                with self.subTest(rate=rate, fraction=fraction):
                    elapsed = 60 / rate * fraction
                    bucket = limits._Bucket(0, 0)
                    self.assertFalse(bucket.take(elapsed, rate, 1))
                    wait = bucket.retry_after(rate)
                    self.assertGreaterEqual(wait, 1)
                    self.assertTrue(bucket.take(elapsed + wait, rate, 1))

    def test_logging_failure_cannot_change_admission_or_leak_permits(self):
        controller = limits.AdmissionController(clock=lambda: 10)
        controller.acquire_global("chat")
        controller.acquire_user("synthetic", "chat")
        with patch.object(observability, "record_timing", side_effect=RuntimeError("synthetic-private-log-error")):
            with self.assertRaises(HTTPException) as caught:
                controller.acquire_user("synthetic", "chat")
            self.assertEqual(caught.exception.status_code, 429)
        controller.release(limits._RequestState(controller, "chat", None, user_id="synthetic"))
        self.assertEqual(controller.active, 0)


class DatabaseReadDiagnosticTests(unittest.TestCase):
    def test_read_categories_never_copy_private_exception_fields(self):
        private = "SYNTHETIC_PRIVATE_MESSAGE_URL_TOKEN_SQL"
        cases = [
            (httpx.PoolTimeout(private), "pool_timeout"),
            (httpx.ConnectTimeout(private), "connect_timeout"),
            (httpx.ReadTimeout(private), "read_timeout"),
            (httpx.WriteTimeout(private), "write_timeout"),
            (httpx.TimeoutException(private), "timeout"),
            (httpx.RemoteProtocolError(private), "protocol_error"),
            (httpx.ConnectError(private), "connection_error"),
            (httpx.TransportError(private), "transport_error"),
            (ValueError(private), "unexpected_error"),
            (persistence.PersistenceUnavailable(), "validation_or_ownership"),
        ]
        for code, expected in (("42501", "upstream_permission"), ("42P01", "upstream_schema"),
                               ("57014", "upstream_timeout"), ("PGRST301", "upstream_auth"),
                               (private, "upstream_api"), ([], "upstream_api")):
            cases.append((APIError({"code": code, "message": private, "details": private, "hint": private}), expected))
        request = httpx.Request("GET", "https://synthetic.invalid/private?token=" + private)
        for status, expected in ((429, "upstream_rate"), (401, "upstream_auth"), (403, "upstream_permission"),
                                 (503, "upstream_unavailable"), (404, "upstream_http")):
            cases.append((httpx.HTTPStatusError(private, request=request,
                          response=httpx.Response(status, request=request)), expected))
        for error, expected in cases:
            with self.subTest(expected=expected), patch.object(observability, "record_timing") as record:
                with self.assertRaises(persistence.PersistenceUnavailable):
                    with persistence.database_operation(read_operation="conversation_history"):
                        raise error
                record.assert_called_once()
                self.assertEqual(record.call_args.args[0], "db.read.failure")
                self.assertEqual(record.call_args.kwargs, {"read_operation": "conversation_history",
                    "category": expected, "outcome": "error"})
                self.assertNotIn(private, json.dumps(record.call_args.kwargs))

    def test_unknown_errors_are_not_stringified_and_operation_names_are_sanitized(self):
        class Unprintable(Exception):
            def __str__(self):
                raise AssertionError("Never inspect private error text")
        with patch.object(observability, "record_timing") as record:
            with self.assertRaises(persistence.PersistenceUnavailable):
                with persistence.database_operation(read_operation="SYNTHETIC_PRIVATE_OPERATION"):
                    raise Unprintable()
        self.assertEqual(record.call_args.kwargs, {"read_operation": "unknown",
            "category": "unexpected_error", "outcome": "error"})

    def test_logging_and_classification_failures_preserve_original_503(self):
        original = persistence.PersistenceUnavailable()
        for target in ("app.observability.record_timing", "app.services.persistence._read_failure_category"):
            with self.subTest(target=target), patch(target, side_effect=RuntimeError("synthetic-private")):
                with self.assertRaises(persistence.PersistenceUnavailable) as caught:
                    with persistence.database_operation(read_operation="conversation_history"):
                        raise original
                self.assertIs(caught.exception, original)

    def test_writes_and_successful_reads_do_not_generate_failure_events(self):
        with patch.object(observability, "record_timing") as record:
            with persistence.database_operation(read_operation="conversation_history"):
                pass
            with self.assertRaises(persistence.PersistenceUnavailable):
                with persistence.database_operation():
                    raise httpx.ReadTimeout("synthetic-private-ambiguous-write")
        record.assert_not_called()


class DiagnosticSinkTests(LoggingTestMixin, unittest.TestCase):
    def test_concurrent_rejections_keep_request_ids_and_never_log_user_identity(self):
        controller = limits.AdmissionController()
        controller.acquire_user("SYNTHETIC_PRIVATE_USER", "chat")
        def reject(index):
            token = observability.request_id.set("synthetic-request-" + str(index))
            try:
                with self.assertRaises(HTTPException):
                    controller.acquire_user("SYNTHETIC_PRIVATE_USER", "chat")
                with self.assertRaises(persistence.PersistenceUnavailable):
                    with persistence.database_operation(read_operation="conversation_history"):
                        raise httpx.ReadTimeout("SYNTHETIC_PRIVATE_USER")
            finally:
                observability.request_id.reset(token)
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(reject, range(16)))
        entries = self.wait_for_entries(32)
        self.assertEqual({e["request_id"] for e in entries}, {"synthetic-request-" + str(i) for i in range(16)})
        self.assertTrue(all(e["reason"] == "user_concurrency" for e in entries if e["operation"] == "admission.reject"))
        self.assertTrue(all(e["category"] == "read_timeout" for e in entries if e["operation"] == "db.read.failure"))
        self.assertNotIn("SYNTHETIC_PRIVATE_USER", json.dumps(entries))
        self.assertIsNone(observability.request_id.get())

    def test_slow_sink_and_full_queue_cannot_block_admission_or_read_failure(self):
        entered, release = Event(), Event()
        class SlowHandler(logging.Handler):
            def emit(self, record):
                entered.set()
                release.wait(3)
        self.assertTrue(observability.shutdown_performance_logging(timeout=2))
        observability.logger.handlers = [SlowHandler()]
        observability.configure_performance_logging(True, queue_capacity=2)
        controller = limits.AdmissionController(max_active=0)
        def work():
            for _ in range(32):
                with self.assertRaises(HTTPException):
                    controller.acquire_global("chat")
                with self.assertRaises(persistence.PersistenceUnavailable):
                    with persistence.database_operation(read_operation="conversation_history"):
                        raise httpx.ReadTimeout("synthetic-private-error")
        try:
            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(work)
                self.assertTrue(entered.wait(1))
                # The sink is still blocked; the request-side work must finish.
                future.result(timeout=1)
                self.assertFalse(release.is_set())
                self.assertGreater(observability._worker.dropped, 0)
        finally:
            release.set()
