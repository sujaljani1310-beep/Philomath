"""Real SDK persistence failures must never become another generation attempt."""

from contextlib import ExitStack
import json
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import httpx
from fastapi.testclient import TestClient

from app.models.schemas import ChatRequest
from app.routes import chat
from app.services import conversation_service as conversations, integration_service as integrations
from app.services.auth_service import AuthenticatedUser, get_current_user
from app.services.persistence import PersistenceUnavailable
from tests.synthetic_database import SyntheticDatabase


ANSWER = "A complete synthetic answer with enough detail for the existing quality policy."


def decision(*providers):
    return SimpleNamespace(
        blocked=False, ranked=[SimpleNamespace(key=key, model_used=chat.MODEL_USED[key]) for key in providers],
        confidence=SimpleNamespace(level="High", value=0.8), dominant_category="general",
        reasoning="Synthetic routing.", hard_requirements_unmet=[],
    )


class ChatPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.db = SyntheticDatabase()
        self.db.seed_conversation()
        self.addCleanup(self.db.http.close)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        for module in (conversations, integrations):
            self.stack.enter_context(patch.object(module, "require_supabase", return_value=self.db.client))
        self.stack.enter_context(patch.object(chat, "is_supabase_connected", return_value=True))
        self.stack.enter_context(patch.object(chat, "get_user_provider_keys", return_value={key: "synthetic-key" for key in chat.MODEL_USED}))
        self.stack.enter_context(patch.object(chat, "needs_google_search", return_value=False))
        self.stack.enter_context(patch.object(chat, "brain_route", return_value=decision("openrouter", "gemini", "cerebras")))
        self.provider = self.stack.enter_context(patch.object(chat, "_call_provider", return_value=ANSWER))

    def send(self, mode="basic", conversation="chat"):
        return chat.send_chat(ChatRequest(message="Synthetic question", mode=mode, provider="openrouter", conversation_id=conversation), AuthenticatedUser("owner"))

    def reset(self):
        self.db.rows["messages"].clear()
        self.db.requests.clear()
        self.provider.reset_mock()

    def message_posts(self):
        return [r for r in self.db.requests if r.method == "POST" and r.url.path.endswith("/messages")]

    def failure(self, role, stage="before"):
        def hook(phase, request, body):
            if phase == stage and request.method == "POST" and request.url.path.endswith("/messages") and body["role"] == role:
                return httpx.ReadTimeout("synthetic-private-database-secret", request=request)
        return hook

    def assert_unavailable(self, mode):
        with self.assertRaises(PersistenceUnavailable) as caught:
            self.send(mode)
        self.assertEqual(caught.exception.status_code, 503)
        self.assertNotIn("synthetic-private", caught.exception.detail)
        self.assertEqual(self.provider.call_count, 1)

    def test_write_failures_before_commit_stop_all_three_modes(self):
        for mode in ("basic", "manual", "auto"):
            for role, persisted, attempts in (("user", 0, 1), ("assistant", 1, 2)):
                with self.subTest(mode=mode, role=role):
                    self.reset()
                    self.db.hook = self.failure(role)
                    self.assert_unavailable(mode)
                    self.assertEqual(len(self.db.rows["messages"]), persisted)
                    self.assertEqual(len(self.message_posts()), attempts)
                    self.assertFalse(any(r.method == "HEAD" for r in self.db.requests))

    def test_commits_with_lost_responses_are_not_retried_in_any_mode(self):
        for mode in ("basic", "manual", "auto"):
            for role, persisted in (("user", 1), ("assistant", 2)):
                with self.subTest(mode=mode, role=role):
                    self.reset()
                    self.db.hook = self.failure(role, "after")
                    self.assert_unavailable(mode)
                    self.assertEqual(len(self.db.rows["messages"]), persisted)
                    self.assertEqual(len(self.message_posts()), persisted)

    def test_count_failure_after_two_commits_never_generates_or_writes_again(self):
        for mode in ("basic", "manual", "auto"):
            with self.subTest(mode=mode):
                self.reset()
                self.db.hook = lambda stage, req, body: httpx.ReadTimeout("synthetic-private-count", request=req) if stage == "before" and req.method == "HEAD" else None
                self.assert_unavailable(mode)
                self.assertEqual(len(self.db.rows["messages"]), 2)
                self.assertEqual(len(self.message_posts()), 2)
                self.assertEqual(sum(r.method == "HEAD" for r in self.db.requests), 1)

    def test_missing_write_ack_is_not_treated_as_saved(self):
        for count in (None, 0, 2, -1):
            with self.subTest(count=count):
                self.reset()
                self.db.hook = lambda phase, req, body: httpx.Response(201, headers={} if count is None else {"content-range": f"*/{count}"}) if phase == "after" and req.method == "POST" and req.url.path.endswith("/messages") else None
                self.assert_unavailable("basic")
                self.assertEqual(len(self.db.rows["messages"]), 1)
                self.assertEqual(len(self.message_posts()), 1)

    def test_missing_or_impossible_count_is_not_invented_in_success_response(self):
        for count in (None, 0, 1, -1):
            with self.subTest(count=count):
                self.reset()
                self.db.hook = lambda phase, req, body: httpx.Response(200, headers={} if count is None else {"content-range": f"*/{count}"}) if phase == "before" and req.method == "HEAD" else None
                self.assert_unavailable("basic")
                self.assertEqual(len(self.message_posts()), 2)

    def test_conversation_update_without_affected_row_stops_before_generation(self):
        self.db.hook = lambda phase, req, body: httpx.Response(204, headers={"content-range": "*/0"}) if phase == "before" and req.method == "PATCH" else None
        with self.assertRaises(PersistenceUnavailable):
            self.send()
        self.provider.assert_not_called()
        self.assertFalse(self.message_posts())

    def test_lost_conversation_insert_ack_stops_before_generation(self):
        self.db.hook = lambda phase, req, body: httpx.ReadTimeout("synthetic-private-create", request=req) if phase == "after" and req.method == "POST" and req.url.path.endswith("/conversations") else None
        with self.assertRaises(PersistenceUnavailable):
            self.send(conversation="new-chat")
        self.provider.assert_not_called()
        self.assertTrue(any(row["id"] == "new-chat" for row in self.db.rows["conversations"]))
        self.assertEqual(sum(r.method == "POST" for r in self.db.requests), 1)

    def test_preparation_errors_return_503_without_provider_calls(self):
        for target in ("ensure_conversation_exists", "get_user_provider_keys"):
            with self.subTest(target=target), patch.object(chat, target, side_effect=RuntimeError("synthetic-private-preparation")):
                with self.assertRaises(PersistenceUnavailable):
                    self.send()
        self.provider.assert_not_called()

    def test_history_database_failure_is_not_a_provider_failure(self):
        self.db.hook = lambda phase, req, body: httpx.ReadTimeout("synthetic-private-history", request=req) if phase == "before" and req.method == "GET" and req.url.path.endswith("/messages") else None
        self.provider.side_effect = lambda provider, conversation, message, key, user: conversations.load_recent_messages(conversation, user)
        for mode in ("basic", "manual", "auto"):
            with self.subTest(mode=mode):
                self.reset()
                self.assert_unavailable(mode)
                self.assertFalse(self.message_posts())

    def test_malformed_history_is_not_a_provider_failure_or_cached_success(self):
        for rows in (None, "not-json", {}, [123], [{}], [{"role": None, "content": "text"}], [{"role": "user", "content": None}]):
            for mode in ("basic", "manual", "auto"):
                with self.subTest(rows=rows, mode=mode):
                    self.reset()
                    self.db.hook = lambda phase, req, body: httpx.Response(200, content=json.dumps(rows), headers={"content-type": "application/json"}) if phase == "before" and req.method == "GET" and req.url.path.endswith("/messages") else None
                    self.provider.side_effect = lambda provider, conversation, message, key, user: conversations.load_recent_messages(conversation, user)
                    self.assert_unavailable(mode)
                    self.assertFalse(self.message_posts())
                    self.db.hook = lambda *args: None
                    self.assertEqual(conversations.load_recent_messages("chat", "owner"), [])

    def test_provider_fallback_still_precedes_persistence_and_stops_after_accepted_answer(self):
        for mode in ("basic", "auto"):
            with self.subTest(mode=mode):
                self.reset()
                self.provider.side_effect = [RuntimeError("synthetic provider outage"), ANSWER]
                self.db.hook = self.failure("assistant")
                with self.assertRaises(PersistenceUnavailable):
                    self.send(mode)
                self.assertEqual([call.args[0] for call in self.provider.call_args_list], ["openrouter", "gemini"])
                self.assertEqual(len(self.message_posts()), 2)

    def test_manual_provider_failure_is_persisted_once_without_second_save_attempt(self):
        self.provider.side_effect = RuntimeError("synthetic provider outage")
        self.db.hook = self.failure("user")
        self.assert_unavailable("manual")
        self.assertEqual(len(self.message_posts()), 1)

    def test_repeated_submissions_are_not_claimed_to_have_exactly_once_semantics(self):
        self.db.hook = self.failure("assistant", "after")
        self.assert_unavailable("basic")
        self.db.hook = lambda *args: None
        response = self.send()
        self.assertEqual(self.provider.call_count, 2)  # A distinct, explicit submission.
        self.assertEqual(response["saved_messages"], 4)
        self.assertEqual(len(self.message_posts()), 4)

    def test_generated_answer_can_be_recovered_only_if_the_assistant_write_committed(self):
        self.db.hook = self.failure("assistant", "after")
        self.assert_unavailable("basic")
        self.db.hook = lambda *args: None
        rows = conversations.get_conversation_messages("chat", "owner")
        self.assertEqual(rows[-1]["content"], ANSWER)
        self.assertEqual(conversations.get_conversation_messages("chat", "another-user"), [])

    def test_empty_count_and_provider_failure_paths_also_fail_closed(self):
        with patch.object(chat, "get_user_provider_keys", return_value={}):
            self.db.hook = self.failure("user")
            with self.assertRaises(PersistenceUnavailable):
                self.send()
        self.provider.assert_not_called()

    def test_actual_http_503_does_not_include_answer_or_claim_saved_messages(self):
        from app.main import app
        self.db.hook = self.failure("assistant", "after")
        with patch.dict(app.dependency_overrides, {get_current_user: lambda: AuthenticatedUser("owner")}):
            with TestClient(app) as caller:
                response = caller.post("/api/chat/send", json={"message": "Synthetic", "conversation_id": "chat"})
        self.assertEqual(response.status_code, 503)
        self.assertEqual(set(response.json()), {"detail"})
        self.assertNotIn(ANSWER, response.text)
        self.assertNotIn("saved_messages", response.text)
        self.assertNotIn("synthetic-private", response.text)
        self.provider.assert_called_once()
