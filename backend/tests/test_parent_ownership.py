"""Application guards over the real SDK and synthetic HTTP, not PostgreSQL RLS."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from copy import deepcopy
from threading import Barrier
import unittest
from unittest.mock import patch

import httpx

from app.models.schemas import ChatRequest
from app.routes import chat
from app.services import conversation_service as conversations
from app.services.auth_service import AuthenticatedUser
from app.services.persistence import PersistenceUnavailable
from tests.synthetic_database import SyntheticDatabase
from tests.test_chat_persistence import ANSWER, decision


class ParentOwnershipTests(unittest.TestCase):
    def setUp(self):
        self.db = SyntheticDatabase()
        self.db.seed_conversation()
        self.db.rows["messages"] = [{
            "id": "message", "conversation_id": "chat", "user_id": "owner",
            "role": "assistant", "content": "synthetic private answer",
        }]
        self.addCleanup(self.db.http.close)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(conversations, "require_supabase", return_value=self.db.client))

    def parent(self, owner):
        self.db.rows["conversations"][0]["user_id"] = owner

    def mutations(self):
        return [req for req in self.db.requests if req.method in {"POST", "PATCH", "DELETE"}]

    def test_valid_owner_retains_message_fields_and_count(self):
        self.assertEqual(conversations.get_conversation_messages("chat", "owner"), self.db.rows["messages"])
        self.assertEqual(conversations.load_recent_messages("chat", "owner"), [{"role": "assistant", "content": "synthetic private answer"}])
        self.assertEqual(conversations.count_messages("chat", "owner"), 1)

    def test_foreign_null_or_missing_parent_cannot_authorize_content_or_count(self):
        for owner in ("other", None, "missing"):
            with self.subTest(owner=owner):
                self.db.rows["conversations"].clear()
                if owner != "missing":
                    self.db.seed_conversation(owner)
                self.assertEqual(conversations.get_conversation_messages("chat", "owner"), [])
                for operation in (
                    lambda: conversations.load_recent_messages("chat", "owner"),
                    lambda: conversations.count_messages("chat", "owner"),
                    lambda: conversations.save_message("chat", "owner", "user", "new"),
                ):
                    with self.assertRaises(PersistenceUnavailable):
                        operation()
                self.assertFalse(self.mutations())

    def test_parent_replacement_during_detail_or_history_read_fails_closed(self):
        for read in (conversations.get_conversation_messages, conversations.load_recent_messages):
            with self.subTest(read=read.__name__):
                self.parent("owner")
                self.db.hook = lambda stage, req, body: self.parent("other") if stage == "after" and req.method == "GET" and req.url.path.endswith("/messages") else None
                with self.assertRaises(PersistenceUnavailable):
                    read("chat", "owner")
        self.assertFalse(self.mutations())

    def test_count_is_not_acknowledged_if_parent_changes_during_head(self):
        self.db.hook = lambda stage, req, body: self.parent("other") if stage == "after" and req.method == "HEAD" else None
        with self.assertRaises(PersistenceUnavailable):
            conversations.count_messages("chat", "owner")

    def test_committed_insert_parent_change_is_detected_without_write_replay(self):
        self.db.hook = lambda stage, req, body: self.parent("other") if stage == "after" and req.method == "POST" else None
        with self.assertRaises(PersistenceUnavailable):
            conversations.save_message("chat", "owner", "user", "new")
        self.assertEqual(len(self.db.rows["messages"]), 2)  # No rollback claim.
        self.assertEqual(len(self.mutations()), 1)

    def test_parent_change_after_generation_never_starts_a_fallback(self):
        for mode in ("basic", "manual", "auto"):
            with self.subTest(mode=mode):
                self.parent("owner")
                self.db.requests.clear()
                with patch.object(chat, "is_supabase_connected", return_value=True), patch.object(chat, "get_user_provider_keys", return_value={"gemini": "synthetic-key", "openrouter": "synthetic-key"}), patch.object(chat, "brain_route", return_value=decision("gemini", "openrouter")), patch.object(chat, "_call_provider", side_effect=lambda *args: (self.parent("other"), ANSWER)[1]) as provider:
                    with self.assertRaises(PersistenceUnavailable):
                        chat.send_chat(ChatRequest(message="question", mode=mode, provider="gemini", conversation_id="chat"), AuthenticatedUser("owner"))
                provider.assert_called_once()
                self.assertFalse(any(req.method == "POST" for req in self.db.requests))

    def test_snapshot_cannot_bypass_current_parent_and_failure_invalidates_cache(self):
        with conversations.conversation_history_snapshot():
            conversations.load_recent_messages("chat", "owner")
            self.parent("other")
            with self.assertRaises(PersistenceUnavailable):
                conversations.load_recent_messages("chat", "owner")
            self.parent("owner")
            self.db.rows["messages"][0]["content"] = "new synthetic content"
            self.assertEqual(conversations.load_recent_messages("chat", "owner")[0]["content"], "new synthetic content")
        self.assertEqual(sum(req.method == "GET" and req.url.path.endswith("/messages") for req in self.db.requests), 2)

    def test_history_data_still_reads_once_across_five_fallbacks(self):
        with conversations.conversation_history_snapshot():
            for _ in range(5):
                rows = conversations.load_recent_messages("chat", "owner")
                rows[0]["content"] = "local mutation"
        self.assertEqual(sum(req.method == "GET" and req.url.path.endswith("/messages") for req in self.db.requests), 1)
        self.assertEqual(len(self.db.requests), 7)  # Initial 3, four cache ownership checks.

    def test_delete_foreign_or_null_parent_never_removes_inconsistent_messages(self):
        for owner in (None, "other"):
            with self.subTest(owner=owner):
                self.parent(owner)
                before = deepcopy(self.db.rows)
                with self.assertRaises(PersistenceUnavailable):
                    conversations.delete_conversation("chat", "owner")
                self.assertEqual(self.db.rows, before)
                self.assertFalse(self.mutations())

    def test_delete_absent_user_resource_keeps_idempotent_success_without_writes(self):
        conversations.delete_conversation("chat", "other")
        self.assertFalse(self.mutations())
        self.assertEqual(len(self.db.rows["messages"]), 1)

    def test_detected_null_or_foreign_children_block_all_delete_writes(self):
        for child_owner in (None, "other"):
            with self.subTest(child_owner=child_owner):
                self.db.requests.clear()
                self.db.rows["messages"] = [
                    {"id": "own", "user_id": "owner", "conversation_id": "chat"},
                    {"id": "legacy", "user_id": child_owner, "conversation_id": "chat"},
                ]
                before = deepcopy(self.db.rows)
                with self.assertRaises(PersistenceUnavailable):
                    conversations.delete_conversation("chat", "owner")
                self.assertEqual(self.db.rows, before)
                self.assertFalse(self.mutations())
                cross_owner = [req for req in self.db.requests if req.url.path.endswith("/messages") and "user_id" not in req.url.params]
                self.assertTrue(cross_owner)
                self.assertTrue(all(req.method == "HEAD" and req.url.params["select"] == "id" for req in cross_owner))

    def test_detected_child_insert_after_first_delete_stops_parent_deletion(self):
        def insert(stage, req, body):
            if stage == "after" and req.method == "DELETE" and req.url.path.endswith("/messages"):
                self.db.rows["messages"].append({"id": "legacy", "user_id": None, "conversation_id": "chat"})
        self.db.hook = insert
        with self.assertRaises(PersistenceUnavailable):
            conversations.delete_conversation("chat", "owner")
        self.assertEqual(len(self.db.rows["conversations"]), 1)
        self.assertEqual(self.db.rows["messages"][0]["user_id"], None)
        self.assertEqual([req.url.path.rsplit("/", 1)[-1] for req in self.mutations()], ["messages"])

    def test_missing_child_count_ack_blocks_delete_without_mutation(self):
        self.db.hook = lambda stage, req, body: httpx.Response(200) if stage == "before" and req.method == "HEAD" else None
        with self.assertRaises(PersistenceUnavailable):
            conversations.delete_conversation("chat", "owner")
        self.assertFalse(self.mutations())

    def test_parent_read_outage_is_sanitized_and_does_not_read_messages(self):
        self.db.hook = lambda stage, req, body: httpx.ReadTimeout("synthetic-private-parent-error", request=req) if stage == "before" else None
        with self.assertRaises(PersistenceUnavailable) as caught:
            conversations.get_conversation_messages("chat", "owner")
        self.assertNotIn("synthetic-private", caught.exception.detail)
        self.assertEqual(len(self.db.requests), 1)

    def test_parent_metadata_503_does_not_add_sdk_retries_or_retry_after_waits(self):
        self.db.hook = lambda stage, req, body: httpx.Response(503, json={"code": "synthetic", "message": "synthetic-private", "hint": None, "details": None}, headers={"retry-after": "60"}) if stage == "before" else None
        with patch("postgrest._sync.request_builder.time.sleep") as sleep:
            with self.assertRaises(PersistenceUnavailable):
                conversations.get_conversation_messages("chat", "owner")
        sleep.assert_not_called()
        self.assertEqual(len(self.db.requests), 1)

    def test_malformed_message_identity_is_not_returned(self):
        self.db.hook = lambda stage, req, body: httpx.Response(200, json=[{"conversation_id": "chat", "user_id": "other", "content": "synthetic"}]) if stage == "before" and req.url.path.endswith("/messages") else None
        with self.assertRaises(PersistenceUnavailable):
            conversations.get_conversation_messages("chat", "owner")

    def test_overlapping_users_cannot_read_mismatched_parent_children(self):
        barrier = Barrier(2)
        def read(user):
            barrier.wait(3)
            return conversations.get_conversation_messages("chat", user)
        with ThreadPoolExecutor(max_workers=2) as pool:
            owner, other = list(pool.map(read, ("owner", "other")))
        self.assertEqual(len(owner), 1)
        self.assertEqual(other, [])

    def test_successful_delete_keeps_schema_and_does_not_modify_other_conversations(self):
        self.db.seed_conversation(None, "legacy-parent")
        legacy = {"id": "legacy-child", "conversation_id": "legacy-parent", "user_id": None}
        self.db.rows["messages"].append(legacy)
        conversations.delete_conversation("chat", "owner")
        self.assertEqual(self.db.rows["messages"], [legacy])
        self.assertEqual(self.db.rows["conversations"][0]["id"], "legacy-parent")

    def test_same_process_guards_are_not_claimed_to_prevent_ambiguous_parent_race(self):
        # A constraint is still required: the write can commit after a parent
        # replacement; the application can only reject its acknowledgment.
        self.db.hook = lambda stage, req, body: self.parent("other") if stage == "before" and req.method == "POST" else None
        with self.assertRaises(PersistenceUnavailable):
            conversations.save_message("chat", "owner", "user", "synthetic")
        self.assertEqual(self.db.rows["messages"][-1]["user_id"], "owner")
        self.assertEqual(self.db.rows["conversations"][0]["user_id"], "other")
