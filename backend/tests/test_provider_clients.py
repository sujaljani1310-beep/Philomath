"""Provider contract/lifecycle tests using SDKs over offline HTTP transports."""

import importlib
import asyncio
import concurrent.futures
import json
import subprocess
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx

from app.providers import clients


BACKEND = Path(__file__).resolve().parents[1]


class RecordingTransport(httpx.BaseTransport):
    def __init__(self, status=200):
        self.status = status
        self.requests = []
        self.closed = 0

    def handle_request(self, request):
        self.requests.append({
            "headers": dict(request.headers),
            "body": json.loads(request.content),
            "url": str(request.url),
            "timeout": dict(request.extensions["timeout"]),
        })
        if self.status >= 400:
            body = {"error": {"message": "offline failure", "code": self.status}}
        elif ":generateContent" in request.url.path:
            body = {"candidates": [{
                "content": {"parts": [{"text": "offline answer"}], "role": "model"},
                "finishReason": "STOP",
            }]}
        else:
            body = {"choices": [{"message": {"role": "assistant", "content": "offline answer"}}]}
        return httpx.Response(
            self.status,
            json=body,
            headers={"set-cookie": "provider_session=first-user; Path=/"},
            request=request,
        )

    def close(self):
        self.closed += 1


class CountingResponseStream(httpx.SyncByteStream):
    """Expose actual nested HTTPX stream ownership without a real socket."""

    def __init__(self, fail_read=False):
        self.fail_read = fail_read
        self.closed = 0

    def __iter__(self):
        if self.fail_read:
            raise httpx.ReadError("synthetic response read failure")
        yield b"synthetic response"

    def close(self):
        self.closed += 1


class ProviderClientTests(unittest.TestCase):
    def setUp(self):
        clients.close_provider_clients()
        self.settings = SimpleNamespace(
            provider_timeout_seconds=30,
            provider_connect_timeout_seconds=5,
            provider_max_retries=1,
        )
        self.settings_patch = patch.object(clients, "get_settings", return_value=self.settings)
        self.settings_patch.start()
        self.addCleanup(self.settings_patch.stop)
        self.addCleanup(clients.close_provider_clients)

    def test_provider_modules_do_not_import_heavy_sdks(self):
        script = "\n".join([
            "import importlib, socket, sys",
            "def blocked(*args, **kwargs): raise AssertionError('No network in import test')",
            "socket.socket.connect = blocked",
            "socket.socket.connect_ex = blocked",
            "socket.getaddrinfo = blocked",
            "for name in ['openrouter', 'grok', 'cerebras', 'nvidia', 'gemini']:",
            "    importlib.import_module('app.providers.' + name + '_provider')",
            "assert 'openai' not in sys.modules",
            "assert 'google.genai' not in sys.modules",
        ])
        subprocess.run([sys.executable, "-B", "-c", script], cwd=BACKEND, check=True, capture_output=True)

    def test_openai_compatible_providers_preserve_payloads_and_models(self):
        configurations = [
            ("openrouter", "call_openrouter_basic", "openrouter/free", "Philomath Basic Mode"),
            ("grok", "call_grok", "grok-4.3", "Philomath using Grok"),
            ("cerebras", "call_cerebras", "llama3.1-8b", "Philomath using Cerebras"),
            ("nvidia", "call_nvidia", "meta/llama-3.1-8b-instruct", "Philomath using NVIDIA NIM"),
        ]
        history = [{"role": "user", "content": "earlier"}, {"role": "assistant", "content": "reply"}]
        for name, function_name, model, prompt in configurations:
            with self.subTest(provider=name):
                module = importlib.import_module(f"app.providers.{name}_provider")
                transport = RecordingTransport()
                with (
                    patch.object(clients, "_get_transport", return_value=transport),
                    patch.object(module, "load_recent_messages", return_value=history) as load_history,
                    patch.dict("os.environ", {"GROK_MODEL": "grok-4.3"}),
                ):
                    result = getattr(module, function_name)("conversation", "question", "user-key", "user")
                self.assertEqual(result, "offline answer")
                load_history.assert_called_once_with("conversation", "user", limit=10)
                self.assertEqual(len(transport.requests), 1)
                request = transport.requests[0]
                self.assertEqual(request["headers"]["authorization"], "Bearer user-key")
                self.assertEqual(request["body"]["model"], model)
                self.assertEqual(request["body"]["max_tokens"], 700)
                self.assertIn(prompt, request["body"]["messages"][0]["content"])
                self.assertEqual(request["body"]["messages"][1:], history + [{"role": "user", "content": "question"}])
                self.assertEqual(request["timeout"], {"connect": 5, "read": 30, "write": 30, "pool": 5})
                self.assertEqual(transport.closed, 0)

    def test_shared_pool_keeps_authorization_and_cookies_request_local(self):
        transport = RecordingTransport()
        with patch.object(clients, "_new_transport", return_value=transport) as create_transport:
            for key in ("first-user-key", "second-user-key"):
                with clients.openai_client("https://api.cerebras.ai/v1", key) as client:
                    client.chat.completions.create(model="llama3.1-8b", messages=[])
                self.assertTrue(client.is_closed())
            create_transport.assert_called_once()
            self.assertEqual(set(clients._pools), {"https://api.cerebras.ai"})
            self.assertEqual(transport.closed, 0)
            clients.close_provider_clients()
        self.assertEqual(transport.closed, 1)
        self.assertEqual([r["headers"]["authorization"] for r in transport.requests], [
            "Bearer first-user-key", "Bearer second-user-key",
        ])
        self.assertTrue(all("cookie" not in r["headers"] for r in transport.requests))
        self.assertFalse(clients._pools)
        clients.close_provider_clients()
        self.assertEqual(transport.closed, 1)

    def test_openai_generation_is_not_retried_and_failure_closes_wrapper(self):
        import openai

        transport = RecordingTransport(status=503)
        with (
            patch.object(clients, "_get_transport", return_value=transport),
            patch("openai._base_client.time.sleep"),
        ):
            with self.assertRaises(openai.APIStatusError):
                with clients.openai_client("https://api.x.ai/v1", "test-key") as client:
                    client.chat.completions.create(model="grok-4.3", messages=[])
        self.assertEqual(len(transport.requests), 1)
        self.assertTrue(client.is_closed())
        self.assertEqual(transport.closed, 0)

    def test_generation_retry_after_never_replays_post_or_sleeps(self):
        import openai
        requests = []

        def response(request):
            requests.append(request)
            if len(requests) == 1:
                return httpx.Response(429, headers={"retry-after": "120"}, json={"error": {"message": "synthetic limit"}}, request=request)
            return httpx.Response(200, json={"choices": [{"message": {"content": "answer"}}]}, request=request)

        with (
            patch.object(clients, "_get_transport", return_value=httpx.MockTransport(response)),
            patch("openai._base_client.time.sleep") as sleep,
        ):
            with self.assertRaises(openai.RateLimitError):
                with clients.openai_client("https://provider.invalid/v1", "synthetic-key") as sdk:
                    sdk.chat.completions.create(model="test", messages=[])
        self.assertEqual(len(requests), 1)
        sleep.assert_not_called()

    def test_ambiguous_read_timeout_never_replays_generation_and_releases_wrapper(self):
        import openai
        seen_timeouts = []

        def response(request):
            seen_timeouts.append(dict(request.extensions["timeout"]))
            raise httpx.ReadTimeout("synthetic upstream inactivity", request=request)

        with (
            patch.object(clients, "_get_transport", return_value=httpx.MockTransport(response)),
            patch("openai._base_client.time.sleep") as sleep,
        ):
            with self.assertRaises(openai.APITimeoutError):
                with clients.openai_client("https://provider.invalid/v1", "synthetic-key") as sdk:
                    sdk.chat.completions.create(model="test", messages=[])
        self.assertEqual(seen_timeouts, [{"connect": 5, "read": 30, "write": 30, "pool": 5}])
        sleep.assert_not_called()
        self.assertTrue(sdk.is_closed())

    def test_gemini_preserves_grounding_context_and_timeout_budgets(self):
        from app.providers import gemini_provider

        transport = RecordingTransport()
        history = [{"role": "user", "content": "earlier"}, {"role": "assistant", "content": "reply"}]
        with (
            patch.object(clients, "_get_transport", return_value=transport),
            patch.object(gemini_provider, "get_settings", return_value=self.settings),
            patch.object(gemini_provider, "load_recent_messages", return_value=history),
        ):
            result = gemini_provider.call_gemini_search("conversation", "question", "gemini-test-key", "user")
        self.assertEqual(result, "offline answer")
        self.assertEqual(len(transport.requests), 1)
        request = transport.requests[0]
        self.assertEqual(request["headers"]["x-goog-api-key"], "gemini-test-key")
        self.assertIn("gemini-2.5-flash:generateContent", request["url"])
        self.assertIn("googleSearch", request["body"]["tools"][0])
        prompt = request["body"]["contents"][0]["parts"][0]["text"]
        self.assertIn("USER: earlier\n\nASSISTANT: reply\n\nUSER: question\n\n", prompt)
        self.assertEqual(request["timeout"], {"connect": 5, "read": 30, "write": 30, "pool": 5})
        self.assertEqual(transport.closed, 0)

    def test_gemini_generation_is_not_retried(self):
        from google.genai import errors
        from app.providers import gemini_provider

        transport = RecordingTransport(status=503)
        with (
            patch.object(clients, "_get_transport", return_value=transport),
            patch.object(gemini_provider, "get_settings", return_value=self.settings),
            patch("time.sleep"),
        ):
            with self.assertRaises(errors.ServerError):
                gemini_provider.call_gemini_search("conversation", "question", "gemini-test-key")
        self.assertEqual(len(transport.requests), 1)
        self.assertEqual(transport.closed, 0)

    def test_gemini_gateway_configuration_preserves_explicit_initial_origin(self):
        from google.genai import _base_url
        from app.providers import gemini_provider

        gateway = "https://private-gateway.invalid:8443/custom-prefix"
        for configuration in ("environment", "sdk-default"):
            with self.subTest(configuration=configuration):
                transport = RecordingTransport()
                with (
                    patch.object(clients, "_new_transport", return_value=transport) as construct,
                    patch.object(gemini_provider, "get_settings", return_value=self.settings),
                    patch.object(_base_url, "_default_base_gemini_url", None),
                    patch.object(_base_url, "_default_base_vertex_url", None),
                    patch.dict("os.environ", {"GOOGLE_GEMINI_BASE_URL": gateway if configuration == "environment" else ""}),
                ):
                    if configuration == "sdk-default":
                        _base_url.set_default_base_urls(gateway, None)
                    result = gemini_provider.call_gemini_search("conversation", "question", "gateway-test-key")
                self.assertEqual(result, "offline answer")
                self.assertTrue(transport.requests[0]["url"].startswith(gateway))
                construct.assert_called_once_with("https://private-gateway.invalid:8443")
                self.assertEqual(set(clients._pools), {"https://private-gateway.invalid:8443"})
                clients.close_provider_clients()

    def test_provider_does_not_forward_body_or_api_key_to_redirect_destination(self):
        seen = []
        def redirect(request):
            seen.append(request)
            return httpx.Response(
                307, headers={"location": "https://untrusted.invalid/next"}, request=request
            )

        with patch.object(clients, "_new_transport", return_value=httpx.MockTransport(redirect)):
            with clients.provider_http_client("https://provider.invalid") as client:
                with self.assertRaises(httpx.ProtocolError):
                    client.post("/start", json={"question": "private"}, headers={"x-goog-api-key": "synthetic-key"})
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0].url.host, "provider.invalid")
        self.assertEqual(set(clients._pools), {"https://provider.invalid"})

    def test_shared_origin_cache_is_bounded_and_overflow_pool_is_closed(self):
        transports = [RecordingTransport() for _ in range(3)]
        with (
            patch.object(clients, "_MAX_POOLED_ORIGINS", 2),
            patch.object(clients, "_new_transport", side_effect=transports),
        ):
            for host in ("one.invalid", "two.invalid", "three.invalid"):
                with clients.provider_http_client(f"https://{host}") as client:
                    client.post("/answer", json={"question": "offline"})
        self.assertEqual(set(clients._pools), {"https://one.invalid", "https://two.invalid"})
        self.assertEqual([transport.closed for transport in transports], [0, 0, 1])
        clients.close_provider_clients()
        self.assertEqual([transport.closed for transport in transports], [1, 1, 1])

    def test_native_httpx_proxy_configuration_matches_direct_client_without_network(self):
        # Construct the actual clients/pools; inspect dispatch only, never send.
        # URL-qualified NO_PROXY and scheme-less proxies were regressions in the
        # former urllib reconstruction. HTTPX now owns both normalization and
        # destination matching, TLS verification, and environment certificates.
        configurations = [
            {"HTTPS_PROXY": "proxy.example.invalid:8080"},
            {"HTTPS_PROXY": "http://proxy.example.invalid:8080", "NO_PROXY": "https://api.x.ai"},
            {"ALL_PROXY": "http://proxy.example.invalid:8080", "NO_PROXY": "*.example.invalid,localhost"},
            {"HTTPS_PROXY": "http://proxy.example.invalid:8080", "NO_PROXY": "*"},
        ]
        for environment in configurations:
            with self.subTest(environment=environment), patch.dict("os.environ", environment, clear=True):
                shared = clients._NativeHTTPTransport()
                with httpx.Client() as direct:
                    try:
                        for url in ("https://api.x.ai/v1", "https://other.invalid/v1", "http://localhost/test"):
                            request_url = httpx.URL(url)
                            actual = shared._client._transport_for_url(request_url)
                            expected = direct._transport_for_url(request_url)
                            self.assertEqual(type(actual._pool), type(expected._pool))
                            self.assertEqual(
                                getattr(actual._pool, "_proxy_url", None),
                                getattr(expected._pool, "_proxy_url", None),
                            )
                            self.assertIsNotNone(actual._pool._ssl_context)
                    finally:
                        shared.close()

    def test_overlapping_users_keep_keys_and_cookies_isolated(self):
        import threading
        barrier = threading.Barrier(8)
        recorded = []
        lock = threading.Lock()

        def response(request):
            with lock:
                recorded.append((request.headers.get("authorization"), request.headers.get("cookie")))
            return httpx.Response(
                200, json={"choices": [{"message": {"content": "answer"}}]},
                headers={"set-cookie": "session=" + request.headers["authorization"].split()[-1] + "; Path=/"},
                request=request,
            )

        def request_user(index):
            with clients.openai_client("https://provider.invalid/v1", f"key-{index}") as sdk:
                barrier.wait(timeout=5)
                for _ in range(2):
                    sdk.chat.completions.create(model="test", messages=[])

        with patch.object(clients, "_new_transport", return_value=httpx.MockTransport(response)):
            with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
                list(pool.map(request_user, range(8)))
        self.assertEqual(len(recorded), 16)
        for authorization, cookie in recorded:
            self.assertIn(cookie, (None, "session=" + authorization.split()[-1]))

    def test_native_shared_client_rejects_response_cookie_storage(self):
        def response(request):
            self.assertNotIn("cookie", request.headers)
            return httpx.Response(200, headers={"set-cookie": "shared=unsafe; Path=/"}, request=request)

        with patch.dict("os.environ", {"NO_PROXY": "*"}, clear=True):
            shared = clients._NativeHTTPTransport()
        with patch.object(shared._client, "_transport_for_url", return_value=httpx.MockTransport(response)):
            for key in ("first", "second"):
                request = httpx.Request("POST", "https://provider.invalid", headers={"authorization": key})
                shared.handle_request(request).close()
                self.assertEqual(len(shared._client.cookies), 0)
        shared.close()

    def test_native_layer_rejects_cross_origin_307_before_followup_and_closes_stream(self):
        with patch.dict("os.environ", {"NO_PROXY": "*"}, clear=True):
            native = clients._get_transport("https://provider.invalid")
        requests = []
        stream = CountingResponseStream()

        def response(request):
            requests.append(request)
            return httpx.Response(
                307, headers={"location": "https://untrusted.invalid/target"},
                stream=stream, request=request,
            )

        # Keep both real HTTPX clients and _BorrowedTransport in the path;
        # replace only the innermost network dispatch with an offline transport.
        with patch.object(native._client, "_transport_for_url", return_value=httpx.MockTransport(response)):
            with clients.provider_http_client("https://provider.invalid") as client:
                with self.assertRaises(httpx.ProtocolError):
                    client.post("/start", content=b"synthetic private body", headers={"x-goog-api-key": "synthetic-key"})
        clients.close_provider_clients()
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0].url.host, "provider.invalid")
        self.assertEqual(stream.closed, 1)

    def test_native_layer_same_origin_308_preserves_body_and_closes_each_stream_once(self):
        with patch.dict("os.environ", {"NO_PROXY": "*"}, clear=True):
            native = clients._get_transport("https://provider.invalid")
        requests = []
        streams = [CountingResponseStream(), CountingResponseStream()]

        def response(request):
            requests.append(request)
            first = len(requests) == 1
            return httpx.Response(
                308 if first else 200,
                headers={"location": "/target"} if first else {},
                stream=streams[0] if first else streams[1], request=request,
            )

        with patch.object(native._client, "_transport_for_url", return_value=httpx.MockTransport(response)):
            with clients.provider_http_client("https://provider.invalid") as client:
                response_result = client.post("/start", content=b"synthetic private body", headers={"x-goog-api-key": "synthetic-key"})
        clients.close_provider_clients()
        self.assertEqual(response_result.status_code, 200)
        self.assertEqual([request.method for request in requests], ["POST", "POST"])
        self.assertEqual([request.content for request in requests], [b"synthetic private body"] * 2)
        self.assertEqual([request.headers["x-goog-api-key"] for request in requests], ["synthetic-key"] * 2)
        self.assertEqual([stream.closed for stream in streams], [1, 1])

    def test_native_nested_response_stream_closes_once_on_success_and_read_error(self):
        for fail_read in (False, True):
            with self.subTest(fail_read=fail_read):
                with patch.dict("os.environ", {"NO_PROXY": "*"}, clear=True):
                    native = clients._get_transport("https://provider.invalid")
                stream = CountingResponseStream(fail_read=fail_read)
                requests = []

                def response(request):
                    requests.append(request)
                    return httpx.Response(200, stream=stream, request=request)

                with patch.object(native._client, "_transport_for_url", return_value=httpx.MockTransport(response)):
                    with clients.provider_http_client("https://provider.invalid") as client:
                        if fail_read:
                            with self.assertRaises(httpx.ReadError):
                                client.post("/answer", content=b"synthetic")
                        else:
                            self.assertEqual(client.post("/answer", content=b"synthetic").content, b"synthetic response")
                clients.close_provider_clients()
                self.assertEqual(len(requests), 1)
                self.assertEqual(stream.closed, 1)

    def test_native_layer_overlapping_sdk_users_isolate_auth_headers_and_cookies(self):
        import threading
        with patch.dict("os.environ", {"NO_PROXY": "*"}, clear=True):
            native = clients._get_transport("https://provider.invalid")
        barrier = threading.Barrier(8)
        lock = threading.Lock()
        recorded = []

        def response(request):
            body = json.loads(request.content)
            key = request.headers["authorization"].removeprefix("Bearer ")
            with lock:
                recorded.append((key, request.headers.get("cookie"), body["messages"][0]["content"]))
            return httpx.Response(
                200, json={"choices": [{"message": {"content": "synthetic answer"}}]},
                headers={"set-cookie": f"private={key}; Path=/"}, request=request,
            )

        def user(index):
            with clients.openai_client("https://provider.invalid/v1", f"key-{index}") as sdk:
                barrier.wait(timeout=5)
                for _ in range(2):
                    answer = sdk.chat.completions.create(
                        model="test", messages=[{"role": "user", "content": f"question-{index}"}],
                    )
                    self.assertEqual(answer.choices[0].message.content, "synthetic answer")

        with patch.object(native._client, "_transport_for_url", return_value=httpx.MockTransport(response)):
            with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
                list(pool.map(user, range(8)))
            self.assertEqual(len(native._client.cookies), 0)
        clients.close_provider_clients()
        self.assertEqual(len(recorded), 16)
        for index in range(8):
            own_requests = [(cookie, question) for key, cookie, question in recorded if key == f"key-{index}"]
            self.assertEqual(own_requests, [(None, f"question-{index}"), (f"private=key-{index}", f"question-{index}")])

    def test_shutdown_attempts_every_pool_after_one_close_failure(self):
        from unittest.mock import Mock
        first = Mock()
        first.close.side_effect = RuntimeError("synthetic close failure")
        second = Mock()
        clients._pools.update({"https://first.invalid": first, "https://second.invalid": second})
        with self.assertRaisesRegex(RuntimeError, "synthetic close failure"):
            clients.close_provider_clients()
        first.close.assert_called_once()
        second.close.assert_called_once()
        self.assertFalse(clients._pools)

    def test_gemini_uses_one_app_owned_unused_async_client_and_closes_it(self):
        from google import genai
        from google.genai import types

        async def exercise():
            await clients.close_provider_async_clients()
            shared = clients.gemini_unused_async_client()
            for _ in range(3):
                with clients.provider_http_client("https://provider.invalid") as sync_client:
                    with genai.Client(api_key="synthetic-key", http_options=types.HttpOptions(
                        httpx_client=sync_client, httpx_async_client=shared,
                    )):
                        pass
                self.assertFalse(shared.is_closed)
                self.assertIs(clients.gemini_unused_async_client(), shared)

            def create_sync_sdk(index):
                with clients.provider_http_client("https://provider.invalid") as sync_client:
                    with genai.Client(api_key=f"synthetic-{index}", http_options=types.HttpOptions(
                        httpx_client=sync_client, httpx_async_client=clients.gemini_unused_async_client(),
                    )):
                        self.assertIs(clients.gemini_unused_async_client(), shared)

            with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
                list(pool.map(create_sync_sdk, range(8)))
            self.assertFalse(shared.is_closed)
            with self.assertRaises(NotImplementedError):
                await shared.get("https://provider.invalid")
            await clients.close_provider_async_clients()
            self.assertTrue(shared.is_closed)
            await clients.close_provider_async_clients()

        asyncio.run(exercise())


if __name__ == "__main__":
    unittest.main()
