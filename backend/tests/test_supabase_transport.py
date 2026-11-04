"""Real SDK over mocked HTTP: no session state or credentials cross requests."""

import unittest
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from types import SimpleNamespace
from unittest.mock import patch

import httpx

from app.services import supabase_service as service


class SupabaseTransportTests(unittest.TestCase):
    settings = SimpleNamespace(
        database_timeout_seconds=15,
        auth_timeout_seconds=10,
        connect_timeout_seconds=5,
    )

    def make_client(self, handler):
        # Import before patching so SDK aliases retain the actual HTTPX types.
        import supabase  # noqa: F401

        http_client_class = httpx.Client
        def construct(**kwargs):
            return http_client_class(transport=httpx.MockTransport(handler), **kwargs)
        with patch.object(service, "get_settings", return_value=self.settings), patch(
            "httpx.Client", side_effect=construct
        ):
            client, http_client = service._create_supabase_client(
                "https://database.example.invalid", "synthetic-service-key"
            )
        self.addCleanup(http_client.close)
        return client, http_client

    @staticmethod
    def user_response(user):
        return {
            "id": user, "aud": "authenticated", "app_metadata": {},
            "user_metadata": {}, "created_at": "2026-01-01T00:00:00Z",
        }

    def test_overlapping_users_never_share_auth_cookies_or_bearer_headers(self):
        users = 8
        barrier = Barrier(users)
        requests = []
        def handle(request):
            bearer = request.headers["authorization"]
            requests.append((request.url.path, bearer, request.headers.get("cookie")))
            if request.url.path.endswith("/user"):
                user = bearer.removeprefix("Bearer ")
                # Interleave the first auth requests before setting any cookies.
                if not getattr(local, "first_done", False):
                    local.first_done = True
                    barrier.wait(timeout=5)
                return httpx.Response(
                    200, json=self.user_response(user),
                    headers={"set-cookie": f"gateway_session={user}; Path=/; Secure"},
                )
            return httpx.Response(200, json=[])
        from threading import local as thread_local
        local = thread_local()
        client, http_client = self.make_client(handle)

        def run_user(index):
            user = f"synthetic-user-{index}"
            for _ in range(2):
                self.assertEqual(client.auth.get_user(user).user.id, user)
                client.table("conversations").select("id").eq("user_id", user).execute()
        with ThreadPoolExecutor(max_workers=users) as workers:
            list(workers.map(run_user, range(users)))
        self.assertEqual(len(requests), users * 4)
        self.assertTrue(all(cookie is None for _, _, cookie in requests))
        for path, bearer, _ in requests:
            if path.endswith("/user"):
                self.assertTrue(bearer.startswith("Bearer synthetic-user-"))
            else:
                self.assertEqual(bearer, "Bearer synthetic-service-key")
        self.assertEqual(list(http_client.cookies.jar), [])

    def test_cross_origin_and_downgrade_redirects_never_forward_service_key(self):
        targets = (
            "https://untrusted.example.invalid/rest/v1/conversations",
            "http://database.example.invalid/rest/v1/conversations",
        )
        for target in targets:
            for status in (301, 302, 303, 307, 308):
                with self.subTest(target=target, status=status):
                    requests = []
                    def handle(request):
                        requests.append(request)
                        return httpx.Response(status, headers={"location": target})
                    client, _ = self.make_client(handle)
                    with self.assertRaises(httpx.ProtocolError):
                        client.table("conversations").select("id").execute()
                    self.assertEqual(len(requests), 1)
                    self.assertEqual(requests[0].url.host, "database.example.invalid")

    def test_same_origin_https_redirect_keeps_auth_and_timeout_policy(self):
        requests = []
        def handle(request):
            requests.append(request)
            if len(requests) == 1:
                return httpx.Response(307, headers={"location": "/auth/v1/redirected-user"})
            return httpx.Response(200, json=self.user_response("synthetic-user"))
        client, _ = self.make_client(handle)
        self.assertEqual(client.auth.get_user("synthetic-jwt").user.id, "synthetic-user")
        self.assertEqual(len(requests), 2)
        for request in requests:
            self.assertEqual(request.headers["authorization"], "Bearer synthetic-jwt")
            self.assertEqual(request.extensions["timeout"]["read"], 10)

    def test_unsafe_post_redirect_does_not_replay_message_body(self):
        requests = []
        def handle(request):
            requests.append(request)
            return httpx.Response(308, headers={"location": "https://untrusted.example.invalid/messages"})
        client, _ = self.make_client(handle)
        with self.assertRaises(httpx.ProtocolError):
            client.table("messages").insert({"content": "synthetic private content"}).execute()
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0].method, "POST")
