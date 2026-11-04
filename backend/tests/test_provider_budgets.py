"""Bounded chat validation and generation payloads, without provider networking."""

import importlib
import gzip
import json
import unittest
import zlib
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from pydantic import ValidationError

from app.models.schemas import MAX_CHAT_FILE_CONTEXT_CHARACTERS, ChatRequest
from app.providers import clients, gemini_provider
from app.providers import response_limits
from app.providers.budgets import (
    GEMINI_OUTPUT_TOKENS,
    MAX_PROVIDER_INPUT_CHARS,
    OPENAI_OUTPUT_TOKENS,
    ProviderInputBudgetExceeded,
    validate_provider_input,
)


OPENAI_PROVIDERS = (
    ("cerebras", "call_cerebras"),
    ("grok", "call_grok"),
    ("nvidia", "call_nvidia"),
    ("openrouter", "call_openrouter_basic"),
)


class ChatInputBudgetTests(unittest.TestCase):
    def test_defaults_nullables_and_whitespace_preserved(self):
        request = ChatRequest(message=" \n\t ")
        self.assertEqual(request.message, " \n\t ")
        self.assertEqual(request.mode, "basic")
        self.assertEqual(request.conversation_id, "default")
        self.assertIsNone(request.provider)
        self.assertIsNone(request.file_context)
        self.assertFalse(request.has_files)
        self.assertEqual(ChatRequest(message="", conversation_id=None).message, "")

    def test_unicode_character_limits_at_and_over_boundaries(self):
        # A character bound counts code points rather than UTF-8 bytes. Ingress
        # separately bounds request bytes, including JSON escapes.
        limits = {"message": 32_000, "file_context": 502_000, "conversation_id": 200, "provider": 80}
        for field, maximum in limits.items():
            for char in ("x", "\U0001f642", "\n"):
                with self.subTest(field=field, character=repr(char)):
                    payload = {"message": "question", field: char * maximum}
                    self.assertEqual(getattr(ChatRequest(**payload), field), char * maximum)
                    payload[field] += char
                    with self.assertRaises(ValidationError):
                        ChatRequest(**payload)

    def test_exact_mode_values_validated_early(self):
        for mode in ("basic", "manual", "auto"):
            self.assertEqual(ChatRequest(message="question", mode=mode).mode, mode)
        for mode in ("", "Basic", "BASIC", " auto ", "unknown", None):
            with self.subTest(mode=mode), self.assertRaises(ValidationError):
                ChatRequest(message="question", mode=mode)

    def test_maximum_upload_output_round_trips_to_chat_context(self):
        from fastapi.testclient import TestClient

        from app.main import app
        from app.services.auth_service import AuthenticatedUser

        scenarios = (
            # Previously successful uploads became invalid chat inputs because
            # filenames and separators exceeded the extraction-only allowance.
            [("first.txt", "x" * 250_000), ("second.txt", "y" * 250_000)],
            # Maximum file count, filename lengths, and aggregate extracted
            # text, including multibyte Unicode characters in both fields.
            [("\U0001f642" * 250 + str(index) + ".txt", "\U0001f642" * 100_000) for index in range(5)],
        )
        user = AuthenticatedUser(id="synthetic-upload-owner")
        with (
            patch("app.services.startup_service._read_json", new=AsyncMock(return_value=(200, {"id": user.id}))),
            TestClient(app) as client,
        ):
            for documents in scenarios:
                with self.subTest(file_count=len(documents)):
                    response = client.post("/api/files/upload", files=[
                        ("files", (name, content.encode("utf-8"), "text/plain"))
                        for name, content in documents
                    ], headers={"Authorization": "Bearer synthetic-upload-token"})
                    self.assertEqual(response.status_code, 200, response.text[:200])
                    upload = response.json()
                    self.assertEqual(sum(file["characters"] for file in upload["files"]), 500_000)
                    combined = upload["combined_text"]
                    self.assertGreater(len(combined), 500_000)
                    self.assertLessEqual(len(combined), MAX_CHAT_FILE_CONTEXT_CHARACTERS)
                    request = ChatRequest(
                        message="Summarize these files.", mode="auto",
                        has_files=True, file_context=combined,
                    )
                    self.assertEqual(request.file_context, combined)
                    if len(documents) == 5:
                        self.assertEqual(len(combined), 501_378)


class ProviderInputBudgetTests(unittest.TestCase):
    def test_budget_rejection_does_not_fallback_or_persist_an_error_answer(self):
        from app.routes import chat
        from app.services.auth_service import AuthenticatedUser

        for mode in ("basic", "manual", "auto"):
            with self.subTest(mode=mode):
                request = ChatRequest(
                    message="question", mode=mode, provider="openrouter",
                    conversation_id="synthetic-conversation",
                )
                decision = SimpleNamespace(
                    blocked=False,
                    ranked=[SimpleNamespace(key="openrouter"), SimpleNamespace(key="gemini")],
                )
                with (
                    patch.object(chat, "is_supabase_connected", return_value=True),
                    patch.object(chat, "ensure_conversation_exists"),
                    patch.object(chat, "get_user_provider_keys", return_value={
                        "openrouter": "synthetic-key", "gemini": "synthetic-key",
                    }),
                    patch.object(chat, "needs_google_search", return_value=False),
                    patch.object(chat, "brain_route", return_value=decision),
                    patch.object(chat, "_call_provider", side_effect=ProviderInputBudgetExceeded()) as provider,
                    patch.object(chat, "save_message") as save,
                    patch.object(chat, "count_messages") as count,
                ):
                    with self.assertRaises(ProviderInputBudgetExceeded):
                        chat._send_chat(request, AuthenticatedUser(id="synthetic-user"))
                provider.assert_called_once()
                save.assert_not_called()
                count.assert_not_called()

    def test_incremental_boundary_and_sanitized_rejection(self):
        validate_provider_input(("a" * (MAX_PROVIDER_INPUT_CHARS - 1), "\U0001f642"))
        with self.assertRaises(ProviderInputBudgetExceeded) as caught:
            validate_provider_input(("synthetic-private-content", "x" * MAX_PROVIDER_INPUT_CHARS))
        self.assertEqual(caught.exception.status_code, 422)
        self.assertNotIn("synthetic-private-content", caught.exception.detail)

        def parts():
            yield "x" * (MAX_PROVIDER_INPUT_CHARS + 1)
            raise AssertionError("Oversized input must stop consuming the iterable")

        with self.assertRaises(ProviderInputBudgetExceeded):
            validate_provider_input(parts())

    def test_compatible_builders_include_system_history_and_new_message(self):
        for name, _ in OPENAI_PROVIDERS:
            with self.subTest(provider=name):
                module = importlib.import_module(f"app.providers.{name}_provider")
                build = getattr(module, f"build_{name}_messages")
                system = build("conversation", "")[0]
                new_message = "question"
                remaining = MAX_PROVIDER_INPUT_CHARS - len(system["content"]) - len(new_message)
                history = [{"role": "assistant", "content": "x" * remaining}]
                with patch.object(module, "load_recent_messages", return_value=history):
                    result = build("conversation", new_message, "synthetic-user")
                    self.assertEqual(result, [system, *history, {"role": "user", "content": new_message}])
                    history[0]["content"] += "x"
                    with self.assertRaises(ProviderInputBudgetExceeded):
                        build("conversation", new_message, "synthetic-user")

    def test_all_providers_reject_oversized_history_before_client_creation(self):
        for name, function in (*OPENAI_PROVIDERS, ("gemini", "call_gemini_search")):
            with self.subTest(provider=name):
                module = importlib.import_module(f"app.providers.{name}_provider")
                client_factory = "provider_http_client" if name == "gemini" else "openai_client"
                with (
                    patch.object(module, "load_recent_messages", return_value=[{
                        "role": "user", "content": "x" * (MAX_PROVIDER_INPUT_CHARS + 1),
                    }]),
                    patch.object(module, client_factory) as create_client,
                ):
                    with self.assertRaises(ProviderInputBudgetExceeded):
                        getattr(module, function)("conversation", "question", "synthetic-key", "synthetic-user")
                create_client.assert_not_called()

    def test_gemini_context_preserves_formatting_and_checks_before_join(self):
        history = [{"role": "user", "content": "earlier"}, {"role": "assistant", "content": "reply"}]
        with patch.object(gemini_provider, "load_recent_messages", return_value=history):
            result = gemini_provider.build_gemini_context("conversation", "question", "synthetic-user")
        self.assertEqual(result, "USER: earlier\n\nASSISTANT: reply\n\nUSER: question\n\n")
        framing = len("USER: \n\n")
        result = gemini_provider.build_gemini_context("conversation", "x" * (MAX_PROVIDER_INPUT_CHARS - framing))
        self.assertEqual(len(result), MAX_PROVIDER_INPUT_CHARS)
        with self.assertRaises(ProviderInputBudgetExceeded):
            gemini_provider.build_gemini_context("conversation", "x" * (MAX_PROVIDER_INPUT_CHARS - framing + 1))

    def test_gemini_input_budget_includes_full_prompt(self):
        framing = len("USER: \n\n")
        # The context alone fits; adding system instructions exceeds the budget.
        message = "x" * (MAX_PROVIDER_INPUT_CHARS - framing)
        with patch.object(gemini_provider, "provider_http_client") as create_client:
            with self.assertRaises(ProviderInputBudgetExceeded):
                gemini_provider.call_gemini_search("conversation", message, "synthetic-key")
        create_client.assert_not_called()

    def test_output_caps_and_gemini_grounded_payload_on_real_sdk(self):
        self.assertEqual(OPENAI_OUTPUT_TOKENS, 700)
        self.assertEqual(GEMINI_OUTPUT_TOKENS, 8192)
        requests = []

        def respond(request):
            requests.append(json.loads(request.content))
            return httpx.Response(200, json={"candidates": [{
                "content": {"parts": [{"text": "offline answer"}], "role": "model"},
                "finishReason": "STOP",
            }]}, request=request)

        settings = SimpleNamespace(provider_timeout_seconds=30, provider_max_retries=0)
        clients.close_provider_clients()
        self.addCleanup(clients.close_provider_clients)
        with (
            patch.object(clients, "_get_transport", return_value=httpx.MockTransport(respond)),
            patch.object(gemini_provider, "get_settings", return_value=settings),
        ):
            answer = gemini_provider.call_gemini_search("conversation", "question", "synthetic-key")
        self.assertEqual(answer, "offline answer")
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0]["generationConfig"]["maxOutputTokens"], GEMINI_OUTPUT_TOKENS)
        self.assertIn("googleSearch", requests[0]["tools"][0])
        expected_context = "USER: question\n\n"
        expected_prompt = (
            "\nYou are Philomath Basic Mode.\n\nYour job:\n"
            "- Answer clearly.\n- Keep answers simple and useful.\n"
            "- Use the conversation history to understand follow-up questions.\n"
            "- Use Google Search grounding for current/recent/specific topics.\n"
            "- Do not confuse similarly named things.\n- Do not make up facts.\n"
            "- If you are unsure, say you are unsure.\n\nConversation:\n"
            + expected_context + "\n\nAssistant answer:\n"
        )
        self.assertEqual(requests[0]["contents"][0]["parts"][0]["text"], expected_prompt)

    def test_gemini_does_not_replay_ambiguous_generation_or_honor_retry_after(self):
        from google.genai import errors

        for failure in ("read-timeout", "temporary-unavailable", "rate-limited"):
            with self.subTest(failure=failure):
                requests = []

                def respond(request):
                    requests.append(request)
                    if failure == "read-timeout":
                        raise httpx.ReadTimeout("synthetic timeout", request=request)
                    status = 503 if failure == "temporary-unavailable" else 429
                    return httpx.Response(
                        status, headers={"retry-after": "120"},
                        json={"error": {"message": "synthetic upstream error", "code": status}},
                        request=request,
                    )

                # Even an older configuration requesting retries must not
                # replay a non-idempotent generation operation.
                settings = SimpleNamespace(provider_timeout_seconds=30, provider_max_retries=2)
                with (
                    patch.object(clients, "_get_transport", return_value=httpx.MockTransport(respond)),
                    patch.object(gemini_provider, "get_settings", return_value=settings),
                    patch("time.sleep") as sleep,
                ):
                    with self.assertRaises((httpx.ReadTimeout, errors.APIError)):
                        gemini_provider.call_gemini_search("conversation", "question", "synthetic-key")
                self.assertEqual(len(requests), 1)
                sleep.assert_not_called()


class CountingWireStream(httpx.SyncByteStream):
    def __init__(self, content, chunk_size=17):
        self.content = content
        self.chunk_size = chunk_size
        self.closed = 0
        self.iterations = 0

    def __iter__(self):
        self.iterations += 1
        for offset in range(0, len(self.content), self.chunk_size):
            yield self.content[offset:offset + self.chunk_size]

    def close(self):
        self.closed += 1


class ProviderResponseBudgetTests(unittest.TestCase):
    def setUp(self):
        clients.close_provider_clients()
        settings = SimpleNamespace(
            provider_timeout_seconds=30, provider_connect_timeout_seconds=5,
            provider_max_retries=2,
        )
        self.settings = patch.object(clients, "get_settings", return_value=settings)
        self.settings.start()
        self.addCleanup(self.settings.stop)
        self.addCleanup(clients.close_provider_clients)

    def read_body(self, wire, *, headers=None):
        def respond(request):
            self.assertEqual(request.headers["accept-encoding"], "gzip, deflate")
            return httpx.Response(200, headers=headers or {}, stream=wire, request=request)

        with patch.object(clients, "_get_transport", return_value=httpx.MockTransport(respond)):
            with clients.provider_http_client("https://synthetic-provider.invalid") as client:
                return client.post("/answer", json={"message": "synthetic question"}).content

    def test_decoded_and_encoded_identity_boundaries_release_stream(self):
        for decoded, encoded, length, accepted in ((8, 16, 8, True), (8, 16, 9, False), (16, 8, 8, True), (16, 8, 9, False)):
            with self.subTest(decoded=decoded, encoded=encoded, length=length):
                wire = CountingWireStream(b"x" * length)
                with (
                    patch.object(response_limits, "MAX_DECODED_RESPONSE_BYTES", decoded),
                    patch.object(response_limits, "MAX_ENCODED_RESPONSE_BYTES", encoded),
                ):
                    if accepted:
                        self.assertEqual(self.read_body(wire), b"x" * length)
                    else:
                        with self.assertRaises(httpx.ReadError):
                            self.read_body(wire)
                self.assertEqual(wire.closed, 1)

    def test_compressed_decoded_and_encoded_boundaries_release_stream(self):
        payload = b"synthetic content"
        encoded = gzip.compress(payload)
        for decoded_limit, encoded_limit, accepted in (
            (len(payload), len(encoded), True),
            (len(payload) - 1, len(encoded), False),
            (len(payload), len(encoded) - 1, False),
        ):
            with self.subTest(decoded=decoded_limit, encoded=encoded_limit):
                wire = CountingWireStream(encoded)
                with (
                    patch.object(response_limits, "MAX_DECODED_RESPONSE_BYTES", decoded_limit),
                    patch.object(response_limits, "MAX_ENCODED_RESPONSE_BYTES", encoded_limit),
                ):
                    if accepted:
                        self.assertEqual(self.read_body(wire, headers={"content-encoding": "gzip"}), payload)
                    else:
                        with self.assertRaises(httpx.ReadError):
                            self.read_body(wire, headers={"content-encoding": "gzip"})
                self.assertEqual(wire.closed, 1)

    def test_gzip_and_both_deflate_variants_preserve_real_sdk_responses(self):
        body = json.dumps({"choices": [{"message": {"content": "offline compressed answer"}}]}).encode()
        raw = zlib.compressobj(wbits=-zlib.MAX_WBITS)
        raw_body = raw.compress(body) + raw.flush()
        for encoding, content in (("gzip", gzip.compress(body)), ("deflate", zlib.compress(body)), ("deflate", raw_body)):
            with self.subTest(encoding=encoding, length=len(content)):
                wire = CountingWireStream(content)

                def respond(request):
                    return httpx.Response(200, headers={"content-encoding": encoding}, stream=wire, request=request)

                with patch.object(clients, "_get_transport", return_value=httpx.MockTransport(respond)):
                    with clients.openai_client("https://synthetic-provider.invalid/v1", "synthetic-key") as sdk:
                        response = sdk.chat.completions.create(model="synthetic", messages=[])
                self.assertEqual(response.choices[0].message.content, "offline compressed answer")
                self.assertEqual(wire.closed, 1)

    def test_deflate_raw_zlib_prefix_and_split_headers_match_httpx(self):
        # A valid raw stream may start with a valid-looking zlib header. A
        # header heuristic rejects this fixture even though HTTPX accepts it.
        raw = b"\x78\x9c\x00\x63\xff" + b"x" * 156 + b"\x01\x00\x00\xff\xff"
        self.assertEqual(zlib.decompress(raw, -zlib.MAX_WBITS), b"x" * 156)
        reference = httpx.Response(200, headers={"content-encoding": "deflate"}, content=raw)
        self.assertEqual(reference.content, b"x" * 156)
        for encoding, content, expected in (
            ("deflate", raw, b"x" * 156),
            ("deflate", zlib.compress(b"split header"), b"split header"),
            ("gzip", gzip.compress(b"split header"), b"split header"),
        ):
            with self.subTest(encoding=encoding, length=len(content)):
                wire = CountingWireStream(content, chunk_size=1)
                self.assertEqual(self.read_body(wire, headers={"content-encoding": encoding}), expected)
                self.assertEqual(wire.closed, 1)

    def test_gzip_bomb_is_rejected_before_sdk_expansion_without_post_replay(self):
        import openai

        content = gzip.compress(b"x" * (response_limits.MAX_DECODED_RESPONSE_BYTES + 1))
        wire = CountingWireStream(content)
        requests = []

        def respond(request):
            requests.append(request)
            return httpx.Response(200, headers={"content-encoding": "gzip"}, stream=wire, request=request)

        with (
            patch.object(clients, "_get_transport", return_value=httpx.MockTransport(respond)),
            patch("openai._base_client.time.sleep") as sleep,
        ):
            with self.assertRaises(openai.APIConnectionError) as caught:
                with clients.openai_client("https://synthetic-provider.invalid/v1", "synthetic-key") as sdk:
                    sdk.chat.completions.create(model="synthetic", messages=[])
        self.assertIsInstance(caught.exception.__cause__, httpx.ReadError)
        self.assertEqual(len(requests), 1)
        sleep.assert_not_called()
        self.assertEqual(wire.closed, 1)

    def test_header_rejections_close_without_reading_or_exposing_content(self):
        for headers in (
            {"content-length": str(response_limits.MAX_DECODED_RESPONSE_BYTES + 1)},
            {"content-encoding": "br"},
            {"content-encoding": "gzip, deflate"},
            {"content-length": "private-invalid-length"},
        ):
            with self.subTest(headers=headers):
                wire = CountingWireStream(b"synthetic-private-response")
                with self.assertRaises(httpx.ReadError) as caught:
                    self.read_body(wire, headers=headers)
                self.assertNotIn("private", str(caught.exception))
                self.assertNotIn("synthetic-provider", str(caught.exception))
                self.assertEqual(wire.iterations, 0)
                self.assertEqual(wire.closed, 1)

    def test_malformed_compression_fails_safely_and_pool_remains_usable(self):
        for content in (b"not compressed", gzip.compress(b"answer")[:-4]):
            wire = CountingWireStream(content)
            with self.assertRaises(httpx.ReadError):
                self.read_body(wire, headers={"content-encoding": "gzip"})
            self.assertEqual(wire.closed, 1)
        for encoding, content in (("gzip", gzip.compress(b"answer")), ("deflate", zlib.compress(b"answer"))):
            with self.subTest(trailing_data=encoding):
                wire = CountingWireStream(content + b"private-invalid-trailing-data")
                with self.assertRaises(httpx.ReadError) as caught:
                    self.read_body(wire, headers={"content-encoding": encoding})
                self.assertNotIn("private", str(caught.exception))
                self.assertEqual(wire.closed, 1)
        healthy = CountingWireStream(b"healthy response")
        self.assertEqual(self.read_body(healthy), b"healthy response")
        self.assertEqual(healthy.closed, 1)

    def test_oversized_same_origin_redirect_body_is_bounded_before_followup(self):
        for status in (307, 308):
            requests = []
            wire = CountingWireStream(b"x" * 9)

            def respond(request):
                requests.append(request)
                return httpx.Response(status, headers={"location": "/next"}, stream=wire, request=request)

            with (
                patch.object(response_limits, "MAX_DECODED_RESPONSE_BYTES", 8),
                patch.object(clients, "_get_transport", return_value=httpx.MockTransport(respond)),
            ):
                with clients.provider_http_client("https://synthetic-provider.invalid") as client:
                    with self.assertRaises(httpx.ReadError):
                        client.post("/start", json={"message": "synthetic question"})
            self.assertEqual(len(requests), 1)
            self.assertEqual(wire.closed, 1)


if __name__ == "__main__":
    unittest.main()
