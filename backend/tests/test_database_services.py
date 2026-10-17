import unittest
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from types import SimpleNamespace
from unittest.mock import Mock, patch

import httpx
from supabase import ClientOptions, create_client

from app.services import conversation_service as conversations
from app.services import integration_service as integrations
from app.services.persistence import PersistenceUnavailable


class DatabaseServiceTests(unittest.TestCase):
    def setUp(self):
        self.requests = []
        self.responses = []

        def handle(request):
            self.requests.append(request)
            return self.responses.pop(0)

        self.http = httpx.Client(transport=httpx.MockTransport(handle))
        self.addCleanup(self.http.close)
        self.client = create_client(
            "https://database.example.invalid", "test-service-key",
            options=ClientOptions(httpx_client=self.http),
        )
        self.patches = [
            patch.object(conversations, "require_supabase", return_value=self.client),
            patch.object(integrations, "require_supabase", return_value=self.client),
        ]
        for active in self.patches:
            active.start()
            self.addCleanup(active.stop)

    def test_count_uses_head_and_exact_count_without_rows(self):
        self.responses = [
            httpx.Response(200, json=[{"id": "chat"}]),
            httpx.Response(200, headers={"content-range": "0-99/100"}),
            httpx.Response(200, json=[{"id": "chat"}]),
        ]
        self.assertEqual(conversations.count_messages("chat", "owner"), 100)
        request = self.requests[1]
        self.assertEqual(request.method, "HEAD")
        self.assertIn("count=exact", request.headers["prefer"])
        self.assertEqual(request.url.params["user_id"], "eq.owner")
        self.assertEqual(request.url.params["conversation_id"], "eq.chat")

    def test_conversation_listing_retains_all_fields_owner_and_order(self):
        rows = [{"id": "chat", "title": "Test", "mode": "basic"}]
        self.responses = [httpx.Response(200, json=rows)]
        self.assertEqual(conversations.get_all_conversations("owner"), rows)
        params = self.requests[0].url.params
        self.assertEqual(params["select"], "*")
        self.assertEqual(params["user_id"], "eq.owner")
        self.assertEqual(params["order"], "updated_at.desc")

    def test_integration_listing_fetches_metadata_without_decrypting(self):
        rows = [{"provider": "gemini", "api_key_last4": "1234"}]
        self.responses = [httpx.Response(200, json=rows)]
        with patch.object(integrations, "_fernet", side_effect=AssertionError("decrypt")):
            self.assertEqual(integrations.list_integrations("owner"), rows)
        params = self.requests[0].url.params
        self.assertEqual(params["user_id"], "eq.owner")
        self.assertNotIn("encrypted_api_key", params["select"])

    def test_message_write_keeps_ownership_check_and_uses_minimal_response(self):
        self.responses = [
            httpx.Response(200, json=[{"id": "chat"}]),
            httpx.Response(201, headers={"content-range": "*/1"}),
            httpx.Response(200, json=[{"id": "chat"}]),
        ]
        conversations.save_message("chat", "owner", "user", "Hello")
        self.assertEqual(len(self.requests), 3)
        check, write, confirmation = self.requests
        self.assertEqual(confirmation.url.params["user_id"], "eq.owner")
        self.assertEqual(check.url.params["user_id"], "eq.owner")
        self.assertEqual(check.url.params["id"], "eq.chat")
        self.assertEqual(write.method, "POST")
        self.assertIn("return=minimal", write.headers["prefer"])
        self.assertIn("count=exact", write.headers["prefer"])

    def test_unowned_conversation_cannot_receive_message(self):
        self.responses = [httpx.Response(200, json=[])]
        with self.assertRaises(PersistenceUnavailable):
            conversations.save_message("chat", "other-user", "user", "Hello")
        self.assertEqual(len(self.requests), 1)


class HistorySnapshotTests(unittest.TestCase):
    def setUp(self):
        self.query = Mock()
        for method in ("table", "select", "eq", "order", "limit", "insert"):
            getattr(self.query, method).return_value = self.query
        self.query.execute.side_effect = lambda: SimpleNamespace(data=[
            {"role": "assistant", "content": "newest"},
            {"role": "user", "content": "oldest"},
        ])
        active = patch.object(conversations, "require_supabase", return_value=self.query)
        active.start()
        self.addCleanup(active.stop)
        # These tests isolate snapshot behavior. Real-SDK parent checks and
        # concurrent ownership changes are covered in test_parent_ownership.
        guard = patch.object(conversations, "_require_owned_parent")
        guard.start()
        self.addCleanup(guard.stop)

    def test_five_fallbacks_share_one_read_and_do_not_share_mutable_results(self):
        with conversations.conversation_history_snapshot():
            for _ in range(5):
                result = conversations.load_recent_messages("chat", "owner")
                self.assertEqual(result[0]["content"], "oldest")
                result[0]["content"] = "mutated by caller"
        self.assertEqual(self.query.execute.call_count, 1)
        conversations.load_recent_messages("chat", "owner")
        self.assertEqual(self.query.execute.call_count, 2)

    def test_user_conversation_limit_and_request_isolation(self):
        with conversations.conversation_history_snapshot():
            conversations.load_recent_messages("chat", "owner")
            conversations.load_recent_messages("chat", "other")
            conversations.load_recent_messages("other", "owner")
            conversations.load_recent_messages("chat", "owner", limit=5)
        with conversations.conversation_history_snapshot():
            conversations.load_recent_messages("chat", "owner")
        self.assertEqual(self.query.execute.call_count, 5)

    def test_empty_history_is_cached_but_failures_are_not(self):
        self.query.execute.side_effect = [RuntimeError("temporarily unavailable"), SimpleNamespace(data=[])]
        with conversations.conversation_history_snapshot():
            with self.assertRaises(PersistenceUnavailable):
                conversations.load_recent_messages("chat", "owner")
            self.assertEqual(conversations.load_recent_messages("chat", "owner"), [])
            self.assertEqual(conversations.load_recent_messages("chat", "owner"), [])
        self.assertEqual(self.query.execute.call_count, 2)

    def test_write_invalidates_snapshot(self):
        history = SimpleNamespace(data=[{"role": "user", "content": "oldest"}])
        self.query.retry.return_value = self.query
        self.query.execute.side_effect = [
            history, SimpleNamespace(data=[{"id": "chat"}]),
            SimpleNamespace(data=[], count=1), history,
        ]
        with conversations.conversation_history_snapshot():
            conversations.load_recent_messages("chat", "owner")
            conversations.save_message("chat", "owner", "user", "new message")
            conversations.load_recent_messages("chat", "owner")
        self.assertEqual(self.query.execute.call_count, 4)

    def test_parallel_chat_requests_have_independent_snapshots(self):
        barrier = Barrier(2)
        def run(_):
            with conversations.conversation_history_snapshot():
                barrier.wait(timeout=3)
                for _ in range(3):
                    conversations.load_recent_messages("chat", "owner")
        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(run, range(2)))
        self.assertEqual(self.query.execute.call_count, 2)


class EncryptionRegressionTests(unittest.TestCase):
    def test_byok_is_encrypted_at_rest_and_only_metadata_is_returned(self):
        import json
        from cryptography.fernet import Fernet
        from unittest.mock import patch
        key = Fernet.generate_key().decode()
        captured = []
        def handle(request):
            if request.method == "GET":
                return httpx.Response(200, json=[])
            payload = json.loads(request.content)
            captured.append(payload)
            return httpx.Response(201, json=[dict(payload, id="integration")])
        with httpx.Client(transport=httpx.MockTransport(handle)) as http:
            client = create_client(
                "https://database.example.invalid", "test-service-key",
                options=ClientOptions(httpx_client=http),
            )
            with patch.object(integrations, "require_supabase", return_value=client), patch.dict(
                "os.environ", {"PHILOMATH_ENCRYPTION_KEY": key}
            ):
                result = integrations.save_integration("owner", "Google Gemini", "local-test-api-key")
                self.assertEqual(integrations._decrypt_api_key(captured[0]["encrypted_api_key"]), "local-test-api-key")
        self.assertEqual(captured[0]["user_id"], "owner")
        self.assertNotEqual(captured[0]["encrypted_api_key"], "local-test-api-key")
        self.assertEqual(result, {"id": "integration", "provider": "gemini", "display_name": "Gemini", "api_key_last4": "-key"})
        self.assertNotIn("encrypted_api_key", result)
