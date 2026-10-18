"""Cross-route ASGI workflows with real SDK parsing and synthetic upstreams."""
from concurrent.futures import ThreadPoolExecutor
import io
import json
from threading import Event
import unittest
from unittest.mock import patch

import httpx
from docx import Document

from tests.workflow_fixture import ANSWER, USER_IDS, WorkflowFixture


class WorkflowTests(unittest.TestCase):
    def test_chat_rate_response_exposes_actual_refill_wait_without_replaying_generation(self):
        with WorkflowFixture() as f:
            clock = [10.0]
            f.controller.clock = lambda: clock[0]
            f.add_provider("openrouter")
            for _ in range(4):
                self.assertEqual(f.send().status_code, 200)
            response = f.send()
            self.assertEqual(response.status_code, 429)
            self.assertIn("Request rate limit exceeded", response.json()["detail"])
            self.assertEqual(response.headers["retry-after"], "3")
            self.assertEqual(len(f.provider_calls), 4)
            self.assertEqual(len(f.db.rows["messages"]), 8)
            clock[0] += 1
            self.assertEqual(f.send().headers["retry-after"], "2")
            clock[0] += 2
            self.assertEqual(f.send().status_code, 200)
            self.assertEqual(len(f.provider_calls), 5)
            self.assertEqual(f.controller.active, 0)

    def test_create_send_switch_modes_history_and_cleanup(self):
        with WorkflowFixture() as f:
            for path in ("/api/conversations", "/api/integrations"):
                self.assertEqual(f.request("GET", path).status_code, 200)
            f.add_provider("openrouter")
            f.add_provider("gemini")
            for mode, provider in (("basic", None), ("manual", "gemini"), ("auto", None)):
                response = f.send(mode, provider)
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.json()["answer"], ANSWER)
                self.assertEqual(response.json()["mode_used"], mode)
            history = f.request("GET", "/api/conversations/synthetic-chat").json()["messages"]
            self.assertEqual(len(history), 6)
            self.assertEqual([row["role"] for row in history], ["user", "assistant"] * 3)
            self.assertEqual(f.request("GET", "/api/conversations").json()["conversations"][0]["id"], "synthetic-chat")
            self.assertEqual(f.request("DELETE", "/api/conversations/synthetic-chat").status_code, 200)
            for provider in ("openrouter", "gemini"):
                self.assertEqual(f.request("DELETE", "/api/integrations/" + provider).status_code, 200)
            self.assertTrue(all(not rows for rows in f.db.rows.values()))
        self.assertEqual(f.controller.active, 0)
        self.assertTrue(f.db.http.is_closed)
        self.assertTrue(all(c.is_closed for c in f.async_clients))

    def test_all_five_provider_adapters_in_manual_mode(self):
        for provider in ("openrouter", "gemini", "cerebras", "nvidia", "grok"):
            with self.subTest(provider=provider), WorkflowFixture() as f:
                f.add_provider(provider)
                result = f.send("manual", provider)
                self.assertEqual(result.status_code, 200, result.text)
                self.assertEqual(result.json()["provider_used"], provider)
                self.assertEqual(result.json()["answer"], ANSWER)
                self.assertEqual(len(f.provider_calls), 1)
                self.assertEqual(len(f.db.rows["messages"]), 2)

    def test_byok_is_encrypted_masked_and_isolated_between_users(self):
        with WorkflowFixture() as f:
            for user in ("a", "b"):
                response = f.add_provider("openrouter", user)
                self.assertNotIn("encrypted_api_key", response.text)
            encrypted = [row["encrypted_api_key"] for row in f.db.rows["user_ai_integrations"]]
            self.assertTrue(all("synthetic-byok" not in value for value in encrypted))
            self.assertNotEqual(encrypted[0], encrypted[1])
            self.assertEqual(f.send().status_code, 200)
            self.assertEqual(f.request("GET", "/api/conversations/synthetic-chat", "b").json()["messages"], [])
            self.assertEqual(f.send(user="b").status_code, 503)
            self.assertEqual(len(f.provider_calls), 1)
            self.assertEqual(f.request("DELETE", "/api/conversations/synthetic-chat", "b").status_code, 200)
            self.assertEqual(len(f.db.rows["messages"]), 2)
            self.assertEqual(f.request("DELETE", "/api/integrations/openrouter", "b").status_code, 200)
            rows = f.db.rows["user_ai_integrations"]
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["user_id"], USER_IDS["synthetic-user-a"])

    def test_basic_provider_outage_falls_back_once_and_saves_once(self):
        with WorkflowFixture() as f:
            f.add_provider("openrouter")
            f.add_provider("gemini")
            f.provider_failures["openrouter"] = [503]
            response = f.send()
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["provider_used"], "gemini")
            self.assertEqual([c["provider"] for c in f.provider_calls], ["openrouter", "gemini"])
            self.assertEqual(len(f.db.rows["messages"]), 2)

    def test_auto_classifier_and_generation_are_separate_and_user_scoped(self):
        with WorkflowFixture() as f:
            f.add_provider("cerebras")
            f.add_provider("grok")
            response = f.send("auto")
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(sum(c["classifier"] for c in f.provider_calls), 1)
            self.assertEqual(sum(not c["classifier"] for c in f.provider_calls), 1)
            self.assertEqual(response.json()["routing"]["category"], "coding")
            self.assertEqual(len(f.db.rows["messages"]), 2)

    def test_text_and_docx_upload_roundtrip_into_chat(self):
        with WorkflowFixture() as f:
            document = Document()
            document.add_paragraph("Synthetic document evidence.")
            stream = io.BytesIO()
            document.save(stream)
            uploaded = f.request("POST", "/api/files/upload", files=[
                ("files", ("note.txt", b"Synthetic text evidence.", "text/plain")),
                ("files", ("note.docx", stream.getvalue(), "application/vnd.openxmlformats-officedocument.wordprocessingml.document")),
            ])
            self.assertEqual(uploaded.status_code, 200, uploaded.text)
            context = uploaded.json()["combined_text"]
            self.assertIn("Synthetic document evidence.", context)
            f.add_provider("openrouter")
            response = f.send("manual", "openrouter", has_files=True, file_context=context)
            self.assertEqual(response.status_code, 200)
            self.assertIn("Synthetic text evidence.", json.dumps(f.provider_calls[-1]["body"]))

    def test_upload_rejection_then_recovery_leaves_no_worker_or_lease(self):
        with WorkflowFixture() as f:
            malformed = f.request("POST", "/api/files/upload", files={"files": ("bad.pdf", b"not a PDF", "application/pdf")})
            self.assertEqual(malformed.status_code, 422)
            f.now += 60
            valid = f.request("POST", "/api/files/upload", files={"files": ("note.txt", b"Recovered.", "text/plain")})
            self.assertEqual(valid.status_code, 200)
        self.assertEqual(f.controller.active, 0)

    def test_auth_outage_invalid_token_and_recovery_keep_distinct_statuses(self):
        with WorkflowFixture() as f:
            for path in ("/api/conversations", "/api/integrations"):
                self.assertEqual(f.http.get(path, headers={"Authorization": "Bearer invalid"}).status_code, 401)
                f.auth_failure = 503
                self.assertEqual(f.request("GET", path).status_code, 503)
                f.auth_failure = None
                self.assertEqual(f.request("GET", path).status_code, 200)

    def test_lost_assistant_write_response_returns_503_without_regeneration(self):
        with WorkflowFixture() as f:
            f.add_provider("openrouter")
            f.add_provider("gemini")
            def lost_reply(phase, request, body):
                if phase == "after" and request.method == "POST" and request.url.path.endswith("/messages") and body["role"] == "assistant":
                    return httpx.ReadTimeout("SYNTHETIC_PRIVATE_LOST_REPLY", request=request)
            f.database_fault = lost_reply
            response = f.send()
            self.assertEqual(response.status_code, 503)
            self.assertNotIn("SYNTHETIC_PRIVATE", response.text)
            self.assertEqual(len(f.provider_calls), 1)
            self.assertEqual(len(f.db.rows["messages"]), 2)
            self.assertEqual(len(f.request("GET", "/api/conversations/synthetic-chat").json()["messages"]), 2)

    def test_overlapping_chat_is_rejected_then_recovers(self):
        with WorkflowFixture() as f:
            f.add_provider("openrouter")
            entered, release = Event(), Event()
            original = f.provider
            def wait(request):
                entered.set()
                if not release.wait(3):
                    raise AssertionError("Synthetic concurrency barrier timed out")
                return original(request)
            with patch("app.providers.clients._get_transport", return_value=httpx.MockTransport(wait)), ThreadPoolExecutor(max_workers=1) as pool:
                first = pool.submit(f.send)
                try:
                    self.assertTrue(entered.wait(3))
                    before_writes = sum(r.method in {"POST", "PATCH", "DELETE"} for r in f.db.requests)
                    rejected = f.send()
                    self.assertEqual(rejected.status_code, 429)
                    self.assertIn("Too many concurrent requests", rejected.json()["detail"])
                    self.assertEqual(sum(r.method in {"POST", "PATCH", "DELETE"} for r in f.db.requests), before_writes)
                    self.assertEqual(len(f.provider_calls), 0)  # First call is still blocked before dispatch.
                finally:
                    release.set()
                self.assertEqual(first.result(timeout=3).status_code, 200)
            self.assertEqual(f.send().status_code, 200)
        self.assertEqual(f.controller.active, 0)

    def test_history_503_keeps_public_error_but_distinguishes_private_categories_and_recovers(self):
        failures = (
            (httpx.ReadTimeout("synthetic-private-timeout"), "read_timeout"),
            (httpx.RemoteProtocolError("synthetic-private-protocol"), "protocol_error"),
            (httpx.Response(503, json={"message": "synthetic-private-upstream"}), "upstream_api"),
            (httpx.Response(200, json=[{"id": "synthetic-invalid-row"}]), "validation_or_ownership"),
        )
        for failure, expected_category in failures:
            with self.subTest(failure=type(failure).__name__), WorkflowFixture() as f:
                f.db.seed_conversation(USER_IDS["synthetic-user-a"], "synthetic-history")
                row = {"id": "synthetic-history-message", "conversation_id": "synthetic-history",
                       "user_id": USER_IDS["synthetic-user-a"], "role": "user", "content": "Synthetic saved turn"}
                f.db.rows["messages"].append(row)
                def failed_read(phase, request, body):
                    if phase == "before" and request.method == "GET" and request.url.path.endswith("/messages"):
                        return failure
                f.database_fault = failed_read
                with patch("app.observability.record_timing") as record:
                    response = f.request("GET", "/api/conversations/synthetic-history")
                self.assertEqual(response.status_code, 503)
                self.assertNotIn("synthetic-private", response.text)
                self.assertEqual(response.json()["detail"],
                    "The database operation could not be confirmed. Reload to check the current state before trying again.")
                events = [call for call in record.call_args_list if call.args[0] == "db.conversation.messages"]
                self.assertEqual(len(events), 1)
                self.assertEqual(events[0].kwargs, {"outcome": "error"})
                diagnoses = [call for call in record.call_args_list if call.args[0] == "db.read.failure"]
                self.assertEqual(len(diagnoses), 1)
                self.assertEqual(diagnoses[0].kwargs, {"read_operation": "conversation_history",
                    "category": expected_category, "outcome": "error"})
                self.assertEqual(f.db.rows["messages"], [row])
                self.assertFalse(any(r.method in {"POST", "PATCH", "DELETE"} for r in f.db.requests))
                f.database_fault = lambda *_: None
                recovered = f.request("GET", "/api/conversations/synthetic-history")
                self.assertEqual(recovered.status_code, 200)
                self.assertEqual(recovered.json()["messages"], [row])
                self.assertEqual(f.controller.active, 0)
