"""Offline regression coverage for chat modes and public response contracts."""

import os
import unittest
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import MagicMock, call, patch


# Importing the application must never read local dotenv credentials in tests.
os.environ["PYTHON_DOTENV_DISABLED"] = "1"
with patch.dict(os.environ, {
    "SUPABASE_URL": "",
    "SUPABASE_SERVICE_ROLE_KEY": "",
    "SUPABASE_KEY": "",
    "OPENROUTER_API_KEY": "",
    "GEMINI_API_KEY": "",
    "GROK_API_KEY": "",
    "CEREBRAS_API_KEY": "",
    "NVIDIA_API_KEY": "",
}):
    from app.models.schemas import ChatRequest
    from app.routes import chat
    from app.services import conversation_service
    from app.services.auth_service import AuthenticatedUser
    from app.services.brain.scorer import (
        RankedModel,
        RoutingConfidence,
        RoutingDecision,
    )


STRONG_ANSWER = "This is a complete, helpful answer with enough detail to be accepted."
USER_ID = "offline-user"
CONVERSATION_ID = "offline-conversation"
PROVIDERS = ("openrouter", "gemini", "cerebras", "nvidia", "grok")
FAKE_KEYS = {provider: f"fake-test-key-{provider}" for provider in PROVIDERS}


def routing_decision(*providers, blocked=False):
    return RoutingDecision(
        ranked=[
            RankedModel(key=provider, model_used=chat.MODEL_USED[provider], score=0.8)
            for provider in providers
        ],
        confidence=RoutingConfidence(level="High", value=0.8),
        dominant_category="coding",
        reasoning="Offline routing decision.",
        blocked=blocked,
        blocked_reason="The required capability is unavailable." if blocked else None,
    )


class ChatRoutingContractsTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.save_message = self.stack.enter_context(patch.object(chat, "save_message"))
        self.count_messages = self.stack.enter_context(
            patch.object(chat, "count_messages", return_value=12)
        )
        self.provider_call = self.stack.enter_context(
            patch.object(chat, "_call_provider", return_value=STRONG_ANSWER)
        )

    def request(self, mode="basic", **kwargs):
        return ChatRequest(
            message="Explain this example.",
            mode=mode,
            conversation_id=CONVERSATION_ID,
            **kwargs,
        )

    def test_basic_nonsearch_preserves_complete_fallback_order(self):
        self.provider_call.side_effect = [RuntimeError("offline failure")] * 4 + [STRONG_ANSWER]
        with patch.object(chat, "needs_google_search", return_value=False):
            response = chat.answer_basic_mode(
                self.request(), CONVERSATION_ID, USER_ID, FAKE_KEYS
            )

        self.assertEqual(
            [entry.args[0] for entry in self.provider_call.call_args_list],
            ["openrouter", "gemini", "cerebras", "nvidia", "grok"],
        )
        self.assertEqual(response["provider_used"], "grok")
        self.assertEqual(response["model_used"], chat.MODEL_USED["grok"])

    def test_basic_search_preserves_complete_fallback_order(self):
        self.provider_call.side_effect = [RuntimeError("offline failure")] * 4 + [STRONG_ANSWER]
        with patch.object(chat, "needs_google_search", return_value=True):
            response = chat.answer_basic_mode(
                self.request(), CONVERSATION_ID, USER_ID, FAKE_KEYS
            )

        self.assertEqual(
            [entry.args[0] for entry in self.provider_call.call_args_list],
            ["gemini", "openrouter", "cerebras", "nvidia", "grok"],
        )
        self.assertEqual(response["provider_used"], "grok")

    def test_basic_weak_answer_falls_back_only_to_connected_provider(self):
        self.provider_call.side_effect = ["I do not know.", STRONG_ANSWER]
        with patch.object(chat, "needs_google_search", return_value=False):
            response = chat.answer_basic_mode(
                self.request(), CONVERSATION_ID, USER_ID,
                {"openrouter": FAKE_KEYS["openrouter"], "nvidia": FAKE_KEYS["nvidia"]},
            )

        self.assertEqual(
            [entry.args[0] for entry in self.provider_call.call_args_list],
            ["openrouter", "nvidia"],
        )
        self.assertEqual(response["answer"], STRONG_ANSWER)
        self.assertEqual(response["provider_used"], "nvidia")
        self.assertEqual(self.save_message.call_count, 2)

    def test_basic_last_weak_answer_is_preserved(self):
        self.provider_call.return_value = "A short answer."
        response = chat.answer_basic_mode(
            self.request(), CONVERSATION_ID, USER_ID, {"gemini": FAKE_KEYS["gemini"]}
        )

        self.assertEqual(response["answer"], "A short answer.")
        self.assertEqual(response["search_grounding"], "enabled")
        self.provider_call.assert_called_once()

    def test_basic_all_failures_preserve_clean_error_response(self):
        self.provider_call.side_effect = [RuntimeError("offline failure"), RuntimeError("429")]
        with patch.object(chat, "needs_google_search", return_value=False):
            response = chat.answer_basic_mode(
                self.request(), CONVERSATION_ID, USER_ID,
                {"openrouter": FAKE_KEYS["openrouter"], "gemini": FAKE_KEYS["gemini"]},
            )

        self.assertEqual(response["provider_used"], "gemini")
        self.assertEqual(response["search_grounding"], "failed")
        self.assertIn("rate-limited or out of quota", response["answer"])
        self.assertNotIn("429", response["answer"])

    def test_basic_does_not_add_file_context(self):
        request = self.request(has_files=True, file_context="Attached test text.")
        chat.answer_basic_mode(
            request, CONVERSATION_ID, USER_ID, {"openrouter": FAKE_KEYS["openrouter"]}
        )

        self.assertEqual(self.provider_call.call_args.args[2], request.message)

    def test_auto_uses_ranked_order_and_retains_fallback_metadata(self):
        decision = routing_decision("grok", "gemini", "cerebras")
        self.provider_call.side_effect = [RuntimeError("offline failure"), "Unsure", STRONG_ANSWER]
        with patch.object(chat, "brain_route", return_value=decision) as brain_route:
            response = chat.answer_auto_mode(
                self.request("auto"), CONVERSATION_ID, USER_ID, FAKE_KEYS
            )

        self.assertEqual(
            [entry.args[0] for entry in self.provider_call.call_args_list],
            ["grok", "gemini", "cerebras"],
        )
        self.assertEqual(response["provider_used"], "cerebras")
        self.assertEqual(response["model_used"], chat.MODEL_USED["cerebras"])
        self.assertEqual(response["routing"]["confidence_value"], 0.8)
        self.assertEqual(response["routing"]["category"], "coding")
        self.assertIn("fallback 'cerebras' was used", response["routing"]["reason"])
        self.assertEqual(
            set(brain_route.call_args.kwargs["available_provider_keys"]), set(FAKE_KEYS)
        )
        self.assertEqual(
            brain_route.call_args.kwargs["classifier_api_key"], FAKE_KEYS["cerebras"]
        )

    def test_auto_blocked_decision_makes_no_provider_request(self):
        decision = routing_decision(blocked=True)
        with patch.object(chat, "brain_route", return_value=decision):
            response = chat.answer_auto_mode(
                self.request("auto"), CONVERSATION_ID, USER_ID, FAKE_KEYS
            )

        self.provider_call.assert_not_called()
        self.assertEqual(response["answer"], decision.blocked_reason)
        self.assertEqual(response["provider_used"], "none")
        self.assertEqual(response["search_grounding"], "disabled")
        self.assertIn("routing", response)

    def test_auto_no_connected_providers_does_not_run_router(self):
        with patch.object(chat, "brain_route") as brain_route:
            response = chat.answer_auto_mode(
                self.request("auto"), CONVERSATION_ID, USER_ID, {}
            )

        brain_route.assert_not_called()
        self.provider_call.assert_not_called()
        self.assertEqual(response["provider_used"], "none")
        self.assertIn("No AI is connected", response["answer"])

    def test_auto_empty_ranked_list_retains_no_ai_response(self):
        with patch.object(chat, "brain_route", return_value=routing_decision()):
            response = chat.answer_auto_mode(
                self.request("auto"), CONVERSATION_ID, USER_ID, FAKE_KEYS
            )

        self.provider_call.assert_not_called()
        self.assertEqual(response["provider_used"], "none")
        self.assertNotIn("routing", response)

    def test_manual_alias_calls_only_selected_provider_and_keeps_file_context(self):
        request = self.request(
            "manual", provider=" NVIDIA NIM ", has_files=True,
            file_context="Attached test text.",
        )
        response = chat.answer_manual_mode(request, CONVERSATION_ID, USER_ID, FAKE_KEYS)

        self.provider_call.assert_called_once_with(
            "nvidia", CONVERSATION_ID, chat.build_provider_message(request),
            FAKE_KEYS["nvidia"], USER_ID,
        )
        self.assertIn("Attached test text.", self.provider_call.call_args.args[2])
        self.assertEqual(response["provider_used"], "nvidia")

    def test_manual_missing_key_does_not_fall_back(self):
        response = chat.answer_manual_mode(
            self.request("manual", provider="gemini"), CONVERSATION_ID, USER_ID,
            {"grok": FAKE_KEYS["grok"]},
        )

        self.provider_call.assert_not_called()
        self.assertEqual(response["provider_used"], "gemini")
        self.assertIn("is not connected", response["answer"])
        self.assertEqual(response["search_grounding"], "disabled")

    def test_manual_provider_failure_does_not_fall_back(self):
        self.provider_call.side_effect = RuntimeError("offline failure")
        response = chat.answer_manual_mode(
            self.request("manual", provider="grok"), CONVERSATION_ID, USER_ID, FAKE_KEYS
        )

        self.provider_call.assert_called_once()
        self.assertEqual(response["provider_used"], "grok")
        self.assertEqual(response["search_grounding"], "failed")

    def test_manual_unknown_provider_retains_validation_response(self):
        response = chat.answer_manual_mode(
            self.request("manual", provider="unsupported"), CONVERSATION_ID, USER_ID, FAKE_KEYS
        )

        self.provider_call.assert_not_called()
        self.assertEqual(response["provider_used"], "none")
        self.assertEqual(
            response["answer"], "Manual Mode needs an AI you already added to your account."
        )

    def test_save_response_preserves_exact_contract_and_message_order(self):
        request = self.request("auto")
        routing_meta = {"category": "coding"}
        response = chat.save_and_return_response(
            request, CONVERSATION_ID, USER_ID, STRONG_ANSWER, "gemini",
            chat.MODEL_USED["gemini"], "enabled", routing_meta,
        )

        self.assertEqual(response, {
            "answer": STRONG_ANSWER,
            "mode_used": "auto",
            "provider_used": "gemini",
            "model_used": chat.MODEL_USED["gemini"],
            "search_grounding": "enabled",
            "conversation_id": CONVERSATION_ID,
            "saved_messages": 12,
            "routing": routing_meta,
        })
        self.assertEqual(self.save_message.call_args_list, [
            call(conversation_id=CONVERSATION_ID, user_id=USER_ID, role="user", content=request.message),
            call(
                conversation_id=CONVERSATION_ID, user_id=USER_ID, role="assistant",
                content=STRONG_ANSWER, provider_used="gemini", model_used=chat.MODEL_USED["gemini"],
            ),
        ])
        self.count_messages.assert_called_once_with(CONVERSATION_ID, USER_ID)

    def test_empty_provider_answer_retains_placeholder_and_omits_routing(self):
        response = chat.save_and_return_response(
            self.request(), CONVERSATION_ID, USER_ID, "  ", "grok",
            chat.MODEL_USED["grok"], "disabled",
        )

        self.assertEqual(
            response["answer"],
            "I could not generate a clear answer for that. Try rephrasing the question.",
        )
        self.assertNotIn("routing", response)
        self.assertEqual(self.save_message.call_args.kwargs["content"], response["answer"])

    def test_send_chat_with_no_providers_does_not_load_history(self):
        with (
            patch.object(chat, "is_supabase_connected", return_value=True),
            patch.object(chat, "ensure_conversation_exists") as ensure_conversation,
            patch.object(chat, "get_user_provider_keys", return_value={}),
            patch.object(conversation_service, "load_recent_messages") as load_history,
        ):
            response = chat.send_chat(self.request(), AuthenticatedUser(id=USER_ID))

        ensure_conversation.assert_called_once()
        self.provider_call.assert_not_called()
        load_history.assert_not_called()
        self.assertEqual(response["provider_used"], "none")
        self.assertEqual(response["saved_messages"], 12)


class RequestHistoryIntegrationTests(unittest.TestCase):
    def test_send_chat_reuses_history_across_fallbacks_and_resets_between_requests(self):
        query = MagicMock()
        for operation in ("table", "select", "eq", "order", "limit", "retry"):
            getattr(query, operation).return_value = query
        projection = [None]
        history_reads = []
        def select(columns):
            projection[0] = columns
            return query
        def execute():
            if projection[0] == "id":
                return SimpleNamespace(data=[{"id": CONVERSATION_ID}])
            history_reads.append(True)
            return SimpleNamespace(data=[{"role": "user", "content": "Earlier question"}])
        query.select.side_effect = select
        query.execute.side_effect = execute
        seen_history = []
        attempts = []

        def provider(provider_name, answer):
            def invoke(conversation_id, message, api_key=None, user_id=None):
                attempts.append(provider_name)
                seen_history.append(conversation_service.load_recent_messages(conversation_id, user_id))
                return answer
            return invoke

        provider_map = {
            "openrouter": provider("openrouter", "Short"),
            "gemini": provider("gemini", STRONG_ANSWER),
        }
        with (
            patch.object(chat, "is_supabase_connected", return_value=True),
            patch.object(chat, "ensure_conversation_exists"),
            patch.object(chat, "get_user_provider_keys", return_value={
                key: FAKE_KEYS[key] for key in provider_map
            }),
            patch.object(chat, "save_message"),
            patch.object(chat, "count_messages", return_value=4),
            patch.object(chat, "needs_google_search", return_value=False),
            patch.dict(chat.PROVIDER_CALL_MAP, provider_map, clear=True),
            patch.object(conversation_service, "require_supabase", return_value=query),
        ):
            request = ChatRequest(message="Explain this example.", conversation_id=CONVERSATION_ID)
            first = chat.send_chat(request, AuthenticatedUser(id=USER_ID))
            self.assertEqual(len(history_reads), 1)
            second = chat.send_chat(request, AuthenticatedUser(id=USER_ID))
            self.assertEqual(len(history_reads), 2)

        self.assertEqual(attempts, ["openrouter", "gemini", "openrouter", "gemini"])
        self.assertEqual(first["answer"], STRONG_ANSWER)
        self.assertEqual(first["provider_used"], "gemini")
        self.assertEqual(second, first)
        self.assertEqual(seen_history, [[{"role": "user", "content": "Earlier question"}]] * 4)


if __name__ == "__main__":
    unittest.main()
