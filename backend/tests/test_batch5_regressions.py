"""Confirmed Batch 5 defects reproduced without credentials or network."""
from contextlib import ExitStack
import json
import os
import unittest
from unittest.mock import patch

import httpx

from app.models.schemas import ChatRequest
from app.providers import clients, grok_provider
from app.routes import chat
from app.services.auth_service import AuthenticatedUser
from app.services.brain import model_registry, router_brain
from app.services.brain.context import RoutingContext
from app.services.persistence import PersistenceUnavailable


ANSWER = "A complete synthetic answer with enough detail for the existing quality policy."


class Batch5Regressions(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.dict(os.environ, {"GROK_MODEL": "synthetic-alternate-model"}))
        self.stack.enter_context(patch.object(chat, "save_message"))
        self.stack.enter_context(patch.object(chat, "count_messages", return_value=2))

    def test_actual_grok_request_response_and_saved_metadata_match_all_modes(self):
        requests = []
        def provider(request):
            body = json.loads(request.content)
            requests.append(body)
            return httpx.Response(200, json={"id": "synthetic", "object": "chat.completion",
                "created": 1, "model": body["model"],
                "choices": [{"index": 0, "message": {"role": "assistant", "content": ANSWER}, "finish_reason": "stop"}]})
        with patch.object(clients, "_get_transport", return_value=httpx.MockTransport(provider)), \
             patch.object(grok_provider, "load_recent_messages", return_value=[]):
            for mode, handler in (("basic", chat.answer_basic_mode),
                                  ("manual", chat.answer_manual_mode), ("auto", chat.answer_auto_mode)):
                with self.subTest(mode=mode):
                    request = ChatRequest(message="Explain a useful idea.", mode=mode, provider="grok")
                    result = handler(request, "synthetic-chat", "synthetic-owner", {"grok": "synthetic-only-key"})
                    self.assertEqual(result["model_used"], "synthetic-alternate-model")
                    self.assertEqual(requests[-1]["model"], result["model_used"])
                    self.assertEqual(chat.save_message.call_args.kwargs["model_used"], result["model_used"])
                    self.assertEqual(result["provider_used"], "grok")
        self.assertEqual(len(requests), 3)

    def test_failed_grok_metadata_matches_configuration_in_all_modes(self):
        with patch.object(chat, "_call_provider", side_effect=RuntimeError("synthetic-provider-failure")):
            for mode, handler in (("basic", chat.answer_basic_mode),
                                  ("manual", chat.answer_manual_mode), ("auto", chat.answer_auto_mode)):
                with self.subTest(mode=mode):
                    result = handler(ChatRequest(message="Explain.", mode=mode, provider="grok"),
                                     "synthetic-chat", "synthetic-owner", {"grok": "synthetic-only-key"})
                    self.assertEqual(result["model_used"], "synthetic-alternate-model")
                    self.assertEqual(result["search_grounding"], "failed")

    def test_registry_resolution_does_not_mutate_global_model_or_rankings(self):
        original = model_registry.MODEL_REGISTRY["grok"].model_used
        with patch.object(router_brain, "classify_ambiguous_request", return_value=None):
            alternate = router_brain.route(RoutingContext("Write a Python function."))
            with patch.dict(os.environ, {"GROK_MODEL": "grok-4.3"}):
                default = router_brain.route(RoutingContext("Write a Python function."))
        self.assertEqual([(m.key, m.score) for m in alternate.ranked],
                         [(m.key, m.score) for m in default.ranked])
        self.assertEqual(model_registry.MODEL_REGISTRY["grok"].model_used, original)
        self.assertEqual(next(m.model_used for m in alternate.ranked if m.key == "grok"),
                         "synthetic-alternate-model")

    def test_unexpected_routing_error_never_echoes_private_exception(self):
        with patch.object(chat, "is_supabase_connected", return_value=True), \
             patch.object(chat, "ensure_conversation_exists"), \
             patch.object(chat, "get_user_provider_keys", return_value={"grok": "synthetic-only-key"}), \
             patch.object(chat, "brain_route", side_effect=RuntimeError("SYNTHETIC_PRIVATE_EXCEPTION_SENTINEL")), \
             patch.object(chat, "_call_provider") as provider:
            result = chat.send_chat(ChatRequest(message="Explain.", mode="auto"), AuthenticatedUser("owner"))
        self.assertEqual(result["answer"], "Something went wrong. Please try again.")
        self.assertEqual(result["search_grounding"], "failed")
        self.assertNotIn("SYNTHETIC_PRIVATE", json.dumps(result))
        provider.assert_not_called()
        chat.save_message.assert_not_called()

    def test_inferred_file_does_not_discard_satisfiable_search_constraint(self):
        message = "Fix this python API function exception error using the latest version in the attached document."
        with patch.object(router_brain, "classify_ambiguous_request") as classify:
            result = router_brain.route(RoutingContext(message), available_provider_keys=model_registry.MODEL_REGISTRY,
                                         classifier_api_key="synthetic-only-classifier-key")
        self.assertEqual([m.key for m in result.ranked], ["gemini"])
        self.assertTrue(result.hard_requirements_unmet)
        self.assertIn("'file'", result.reasoning)
        self.assertNotIn("'search, file'", result.reasoning)
        classify.assert_not_called()

    def test_combined_inferred_requirements_without_search_provider_stay_best_effort(self):
        result = router_brain.route(RoutingContext("Find the latest information in the attached document."),
                                    available_provider_keys=["grok"])
        self.assertEqual([m.key for m in result.ranked], ["grok"])
        self.assertTrue(result.hard_requirements_unmet)

    def test_confirmed_raw_file_requirement_still_blocks_instead_of_relaxing(self):
        result = router_brain.route(RoutingContext("Find latest information.", has_files=True))
        self.assertTrue(result.blocked)
        self.assertEqual(result.ranked, [])

    def test_preparation_failure_still_raises_sanitized_503(self):
        with patch.object(chat, "is_supabase_connected", return_value=True), \
             patch.object(chat, "ensure_conversation_exists", side_effect=ValueError("SYNTHETIC_PRIVATE_EXCEPTION_SENTINEL")):
            with self.assertRaises(PersistenceUnavailable) as caught:
                chat.send_chat(ChatRequest(message="Explain.", mode="basic"), AuthenticatedUser("owner"))
        self.assertEqual(caught.exception.status_code, 503)
        self.assertNotIn("SYNTHETIC_PRIVATE", caught.exception.detail)
