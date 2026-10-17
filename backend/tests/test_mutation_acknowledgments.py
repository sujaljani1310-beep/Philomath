"""Mutation acknowledgments, ambiguous outcomes and same-process coordination.

The peer exercises the installed Supabase/PostgREST clients without sockets.
It does not emulate PostgreSQL transactions, RLS or multiple app processes.
"""

from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
import json
from threading import Barrier, Event
import unittest
from unittest.mock import patch

from cryptography.fernet import Fernet
import httpx
from fastapi.testclient import TestClient

from app.models.schemas import ChatRequest
from app.routes import chat
from app.services import conversation_service as conversations, integration_service as integrations
from app.services import persistence
from app.services.auth_service import AuthenticatedUser, get_current_user
from app.services.persistence import PersistenceUnavailable, mutation_guard
from tests.synthetic_database import SyntheticDatabase


class MutationAcknowledgmentTests(unittest.TestCase):
    def setUp(self):
        self.db = SyntheticDatabase()
        self.db.seed_conversation()
        self.addCleanup(self.db.http.close)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        for module in (conversations, integrations):
            self.stack.enter_context(patch.object(module, "require_supabase", return_value=self.db.client))
        self.stack.enter_context(patch.dict("os.environ", {"PHILOMATH_ENCRYPTION_KEY": Fernet.generate_key().decode()}))

    def save(self, owner="owner", key="synthetic-key-one"):
        return integrations.save_integration(owner, "Google Gemini", key)

    def lost_response(self, table, method, phase):
        return lambda stage, req, body: httpx.ReadTimeout("synthetic-private-database-error", request=req) if stage == phase and req.method == method and req.url.path.endswith("/" + table) else None

    def test_atomic_integration_upsert_preserves_id_and_only_returns_metadata(self):
        first = self.save()
        second = self.save(key="synthetic-key-two")
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(second, {"id": first["id"], "provider": "gemini", "display_name": "Gemini", "api_key_last4": "-two"})
        self.assertEqual(len(self.db.rows["user_ai_integrations"]), 1)
        row = self.db.rows["user_ai_integrations"][0]
        self.assertNotEqual(row["encrypted_api_key"], "synthetic-key-two")
        self.assertEqual(integrations._decrypt_api_key(row["encrypted_api_key"]), "synthetic-key-two")
        self.assertEqual([req.method for req in self.db.requests], ["POST", "POST"])
        for req in self.db.requests:
            self.assertEqual(req.url.params["on_conflict"], "user_id,provider")
            self.assertNotIn("encrypted_api_key", req.url.params["select"])

    def test_integration_upsert_rejects_missing_malformed_and_mismatched_acknowledgments(self):
        valid = {"id": "ack", "user_id": "owner", "provider": "gemini", "display_name": "Gemini", "api_key_last4": "-one"}
        malformed = [None, [], {}, [123], [valid, valid]]
        for field in valid:
            malformed.extend([[{**valid, field: None}], [{**valid, field: "wrong"}]])
        # IDs need to be nonempty strings, not predetermined values.
        malformed.remove([{**valid, "id": "wrong"}])
        malformed.append([{**valid, "id": ""}])
        for rows in malformed:
            with self.subTest(rows=rows):
                self.db.requests.clear()
                self.db.hook = lambda stage, req, body: httpx.Response(201, json=rows) if stage == "after" else None
                with self.assertRaises(PersistenceUnavailable):
                    self.save()
                self.assertEqual(len(self.db.requests), 1)

    def test_integration_save_ambiguous_response_is_not_retried_or_claimed_saved(self):
        for phase, count in (("before", 0), ("after", 1)):
            with self.subTest(phase=phase):
                self.db.rows["user_ai_integrations"].clear()
                self.db.requests.clear()
                self.db.hook = self.lost_response("user_ai_integrations", "POST", phase)
                with self.assertRaises(PersistenceUnavailable):
                    self.save()
                self.assertEqual(len(self.db.rows["user_ai_integrations"]), count)
                self.assertEqual(len(self.db.requests), 1)

    def test_explicit_repeated_integration_save_reuses_existing_unique_row(self):
        self.db.hook = self.lost_response("user_ai_integrations", "POST", "after")
        with self.assertRaises(PersistenceUnavailable):
            self.save()
        saved_id = self.db.rows["user_ai_integrations"][0]["id"]
        self.db.hook = lambda *args: None
        self.assertEqual(self.save()["id"], saved_id)
        self.assertEqual(len(self.db.rows["user_ai_integrations"]), 1)

    def test_integration_delete_checks_absence_and_repeated_delete_is_safe(self):
        self.save()
        self.db.requests.clear()
        integrations.delete_integration("owner", "gemini")
        integrations.delete_integration("owner", "gemini")
        self.assertEqual([req.method for req in self.db.requests], ["DELETE", "HEAD", "DELETE", "HEAD"])
        self.assertEqual(self.db.rows["user_ai_integrations"], [])

    def test_integration_delete_lost_response_is_503_even_if_committed(self):
        self.save()
        self.db.requests.clear()
        self.db.hook = self.lost_response("user_ai_integrations", "DELETE", "after")
        with self.assertRaises(PersistenceUnavailable):
            integrations.delete_integration("owner", "gemini")
        self.assertEqual(self.db.rows["user_ai_integrations"], [])
        self.assertEqual(len(self.db.requests), 1)
        self.db.hook = lambda *args: None
        integrations.delete_integration("owner", "gemini")

    def test_integration_delete_missing_ack_or_failed_absence_check_is_not_success(self):
        for phase, method, response in (("after", "DELETE", httpx.Response(204)), ("before", "HEAD", httpx.Response(200)), ("before", "HEAD", httpx.Response(200, headers={"content-range": "*/1"}))):
            with self.subTest(method=method, response=response):
                self.db.hook = lambda *args: None
                self.save()
                self.db.requests.clear()
                self.db.hook = lambda stage, req, body: response if stage == phase and req.method == method else None
                with self.assertRaises(PersistenceUnavailable):
                    integrations.delete_integration("owner", "gemini")
                self.assertEqual(sum(req.method == "DELETE" for req in self.db.requests), 1)

    def test_external_replacement_between_delete_and_confirmation_is_not_deleted_again(self):
        self.save()
        def replace(stage, req, body):
            if stage == "after" and req.method == "DELETE":
                self.db.rows["user_ai_integrations"].append({"id": "external-new", "user_id": "owner", "provider": "gemini"})
        self.db.hook = replace
        with self.assertRaises(PersistenceUnavailable):
            integrations.delete_integration("owner", "gemini")
        self.assertEqual(self.db.rows["user_ai_integrations"][0]["id"], "external-new")
        self.assertEqual(sum(req.method == "DELETE" for req in self.db.requests), 1)

    def test_foreign_conversation_cannot_be_updated_or_receive_messages(self):
        original = dict(self.db.rows["conversations"][0])
        with self.assertRaises(PersistenceUnavailable):
            conversations.ensure_conversation_exists("chat", "auto", "intruder", "Changed")
        with self.assertRaises(PersistenceUnavailable):
            conversations.save_message("chat", "intruder", "user", "Foreign message")
        self.assertEqual(self.db.rows["conversations"], [original])
        self.assertEqual(self.db.rows["messages"], [])
        self.assertFalse(any(req.method == "PATCH" for req in self.db.requests))

    def test_malformed_ownership_response_never_authorizes_a_write(self):
        for rows in (None, {}, "text", [123], [{}], [{"id": "other"}], [{"id": "chat"}, {"id": "chat"}]):
            for action in (lambda: conversations.ensure_conversation_exists("chat", "basic", "owner"), lambda: conversations.save_message("chat", "owner", "user", "text")):
                with self.subTest(rows=rows):
                    self.db.requests.clear()
                    self.db.hook = lambda stage, req, body: httpx.Response(200, content=json.dumps(rows), headers={"content-type": "application/json"}) if stage == "before" else None
                    with self.assertRaises(PersistenceUnavailable):
                        action()
                    self.assertEqual([req.method for req in self.db.requests], ["GET"])

    def test_conversation_create_update_missing_or_zero_ack_returns_503(self):
        for conversation in ("chat", "new"):
            for count in (None, 0):
                with self.subTest(conversation=conversation, count=count):
                    self.db.rows["conversations"] = self.db.rows["conversations"][:1]
                    self.db.hook = lambda stage, req, body: httpx.Response(201, headers={} if count is None else {"content-range": "*/0"}) if stage == "after" and req.method in {"POST", "PATCH"} else None
                    with self.assertRaises(PersistenceUnavailable):
                        conversations.ensure_conversation_exists(conversation, "basic", "owner")

    def test_conversation_delete_partial_commits_and_lost_responses_stop_without_replay(self):
        for table in ("messages", "conversations"):
            for phase in ("before", "after"):
                with self.subTest(table=table, phase=phase):
                    self.db.rows["conversations"] = []
                    self.db.seed_conversation()
                    self.db.rows["messages"] = [{"id": "m", "conversation_id": "chat", "user_id": "owner", "role": "user", "content": "text"}]
                    self.db.requests.clear()
                    self.db.hook = self.lost_response(table, "DELETE", phase)
                    with self.assertRaises(PersistenceUnavailable):
                        conversations.delete_conversation("chat", "owner")
                    self.assertEqual(sum(req.method == "DELETE" for req in self.db.requests), 1 if table == "messages" else 2)
                    self.assertEqual(self.db.requests[-1].method, "DELETE")
                    self.assertEqual(len(self.db.rows["messages"]), int(table == "messages" and phase == "before"))
                    self.assertEqual(len(self.db.rows["conversations"]), int(not (table == "conversations" and phase == "after")))

    def test_conversation_delete_does_not_claim_success_on_failed_confirmation(self):
        for table in ("conversations", "messages"):
            for count in (None, 1):
                with self.subTest(table=table, count=count):
                    self.db.rows["conversations"].clear()
                    self.db.seed_conversation()
                    self.db.requests.clear()
                    self.db.hook = lambda stage, req, body: httpx.Response(200, headers={} if count is None else {"content-range": "*/1"}) if stage == "before" and req.method == "HEAD" and req.url.path.endswith("/" + table) and any(previous.method == "DELETE" and previous.url.path.endswith("/conversations") for previous in self.db.requests) else None
                    with self.assertRaises(PersistenceUnavailable):
                        conversations.delete_conversation("chat", "owner")
                    self.assertEqual(sum(req.method == "DELETE" for req in self.db.requests), 2)

    def test_conversation_delete_confirms_absence_and_preserves_other_user_data(self):
        self.db.rows["messages"] = [{"id": "m", "conversation_id": "chat", "user_id": "owner"}]
        conversations.delete_conversation("chat", "intruder")
        self.assertEqual(len(self.db.rows["conversations"]), 1)
        self.assertEqual(len(self.db.rows["messages"]), 1)
        conversations.delete_conversation("chat", "owner")
        conversations.delete_conversation("chat", "owner")
        self.assertEqual(self.db.rows["conversations"], [])
        self.assertEqual(self.db.rows["messages"], [])

    def test_two_users_integration_keys_and_deletes_are_isolated(self):
        self.save("owner", "synthetic-owner-key")
        self.save("other", "synthetic-other-key")
        self.assertEqual(integrations.get_user_provider_keys("owner"), {"gemini": "synthetic-owner-key"})
        self.assertEqual(integrations.get_user_provider_keys("other"), {"gemini": "synthetic-other-key"})
        integrations.delete_integration("owner", "gemini")
        self.assertEqual(integrations.get_user_provider_keys("other"), {"gemini": "synthetic-other-key"})

    def test_concurrent_integration_saves_have_one_row_per_user_and_stable_id(self):
        barrier = Barrier(4)
        def save(index):
            barrier.wait(timeout=3)
            return self.save(f"user-{index % 2}", f"synthetic-key-{index}")
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(save, range(4)))
        self.assertEqual(len(self.db.rows["user_ai_integrations"]), 2)
        self.assertEqual(results[0]["id"], results[2]["id"])
        self.assertEqual(results[1]["id"], results[3]["id"])
        self.assertNotEqual(results[0]["id"], results[1]["id"])

    def test_concurrent_conversation_ensure_same_owner_inserts_only_once(self):
        barrier = Barrier(2)
        def ensure(_):
            barrier.wait(timeout=3)
            conversations.ensure_conversation_exists("new", "basic", "owner")
        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(ensure, range(2)))
        self.assertEqual(sum(req.method == "POST" for req in self.db.requests), 1)
        self.assertEqual(sum(req.method == "PATCH" for req in self.db.requests), 1)
        self.assertEqual(sum(row["id"] == "new" for row in self.db.rows["conversations"]), 1)

    def test_save_pair_and_delete_are_serialized_within_the_process(self):
        inserted = Event()
        release = Event()
        deleting = Event()
        def hook(stage, req, body):
            if stage == "after" and req.method == "POST" and req.url.path.endswith("/messages") and body["role"] == "user":
                inserted.set()
                self.assertTrue(release.wait(3))
        self.db.hook = hook
        def delete():
            deleting.set()
            conversations.delete_conversation("chat", "owner")
        with ThreadPoolExecutor(max_workers=2) as pool:
            saving = pool.submit(chat.save_and_return_response, ChatRequest(message="question"), "chat", "owner", "answer", "gemini", "model", "not_requested")
            try:
                self.assertTrue(inserted.wait(3))
                removing = pool.submit(delete)
                self.assertTrue(deleting.wait(3))
                self.assertFalse(any(req.method == "DELETE" for req in self.db.requests))
            finally:
                release.set()
            self.assertEqual(saving.result(timeout=3)["saved_messages"], 2)
            removing.result(timeout=3)
        methods = [req.method for req in self.db.requests]
        self.assertLess(methods.index("HEAD"), methods.index("DELETE"))
        self.assertEqual(self.db.rows["messages"], [])

    def test_concurrent_integration_save_then_delete_observes_completed_save(self):
        inserted, release, deleting = Event(), Event(), Event()
        def hook(stage, req, body):
            if stage == "after" and req.method == "POST":
                inserted.set()
                self.assertTrue(release.wait(3))
        self.db.hook = hook
        def delete():
            deleting.set()
            integrations.delete_integration("owner", "gemini")
        with ThreadPoolExecutor(max_workers=2) as pool:
            saving = pool.submit(self.save)
            try:
                self.assertTrue(inserted.wait(3))
                removing = pool.submit(delete)
                self.assertTrue(deleting.wait(3))
                self.assertFalse(any(req.method == "DELETE" for req in self.db.requests))
            finally:
                release.set()
            self.assertTrue(saving.result(timeout=3)["id"])
            removing.result(timeout=3)
        self.assertEqual(self.db.rows["user_ai_integrations"], [])

    def test_busy_mutation_guard_fails_boundedly_and_recovers(self):
        held, release = Event(), Event()
        def hold():
            with mutation_guard("integration", ("owner", "gemini")):
                held.set()
                self.assertTrue(release.wait(3))
        with ThreadPoolExecutor(max_workers=1) as pool:
            holder = pool.submit(hold)
            try:
                self.assertTrue(held.wait(3))
                with patch.object(persistence, "MUTATION_LOCK_TIMEOUT_SECONDS", .01):
                    with self.assertRaises(PersistenceUnavailable):
                        self.save()
                self.assertEqual(self.db.requests, [])
            finally:
                release.set()
            holder.result(timeout=3)
        self.assertTrue(self.save()["id"])

    def test_same_conversation_id_from_another_user_does_not_target_owner_guard(self):
        owner_stripe = hash(("conversation", ("owner", "chat"))) % len(persistence._mutation_locks)
        attacker = next(f"other-{i}" for i in range(10000) if hash(("conversation", (f"other-{i}", "chat"))) % len(persistence._mutation_locks) != owner_stripe)
        held, release = Event(), Event()
        def hold():
            with mutation_guard("conversation", (attacker, "chat")):
                held.set()
                self.assertTrue(release.wait(3))
        with ThreadPoolExecutor(max_workers=1) as pool:
            holder = pool.submit(hold)
            try:
                self.assertTrue(held.wait(3))
                with patch.object(persistence, "MUTATION_LOCK_TIMEOUT_SECONDS", .01):
                    conversations.ensure_conversation_exists("chat", "basic", "owner")
                    conversations.save_message("chat", "owner", "user", "question")
                    chat.save_and_return_response(ChatRequest(message="next"), "chat", "owner", "answer", "gemini", "model", "not_requested")
                    conversations.delete_conversation("chat", "owner")
            finally:
                release.set()
            holder.result(timeout=3)

    def test_history_invalidates_after_ambiguous_committed_message_write(self):
        with conversations.conversation_history_snapshot():
            self.assertEqual(conversations.load_recent_messages("chat", "owner"), [])
            self.db.hook = self.lost_response("messages", "POST", "after")
            with self.assertRaises(PersistenceUnavailable):
                conversations.save_message("chat", "owner", "user", "committed")
            self.db.hook = lambda *args: None
            self.assertEqual(conversations.load_recent_messages("chat", "owner"), [{"role": "user", "content": "committed"}])
            with self.assertRaises(PersistenceUnavailable):
                conversations.load_recent_messages("chat", "other")

    def test_mutation_http_failures_are_sanitized_without_success_ack(self):
        from app.main import app
        with patch.dict(app.dependency_overrides, {get_current_user: lambda: AuthenticatedUser("owner")}), TestClient(app) as caller:
            self.db.hook = self.lost_response("user_ai_integrations", "POST", "after")
            save = caller.post("/api/integrations", json={"ai_name": "Gemini", "api_key": "synthetic-key-one"})
            self.db.hook = self.lost_response("user_ai_integrations", "DELETE", "after")
            delete = caller.delete("/api/integrations/gemini")
            self.db.hook = self.lost_response("messages", "DELETE", "after")
            with patch("app.routes.conversations.is_supabase_connected", return_value=True):
                conversation = caller.delete("/api/conversations/chat")
        for response in (save, delete, conversation):
            self.assertEqual(response.status_code, 503)
            self.assertEqual(set(response.json()), {"detail"})
            self.assertNotIn("synthetic-private", response.text)
            self.assertNotIn("synthetic-key-one", response.text)
            self.assertNotIn("success", response.text)

    def test_unconfigured_mutations_do_not_return_false_success(self):
        from app.routes.conversations import remove_conversation
        with patch("app.routes.conversations.is_supabase_connected", return_value=False):
            with self.assertRaises(PersistenceUnavailable):
                remove_conversation("chat", AuthenticatedUser("owner"))
        with patch.object(integrations, "require_supabase", side_effect=RuntimeError("synthetic-missing-config")):
            with self.assertRaises(PersistenceUnavailable):
                self.save()
