import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import httpx
from fastapi.testclient import TestClient

from app.main import app
from app.services import auth_service


class APIContractTests(unittest.TestCase):
    def test_authenticated_list_endpoints_with_native_async_offline_transport(self):
        from app.services import startup_service
        from app.http_safety import stateless_cookies

        requests = []
        def handle(request):
            requests.append(request)
            if request.url.path.endswith("/user"):
                return httpx.Response(200, json={
                    "id": "owner", "aud": "authenticated", "app_metadata": {},
                    "user_metadata": {}, "created_at": "2026-01-01T00:00:00Z",
                })
            if request.url.path.endswith("/conversations"):
                return httpx.Response(200, json=[{"id": "chat", "title": "Test"}])
            return httpx.Response(200, json=[{"provider": "gemini", "api_key_last4": "1234"}])

        upstream = httpx.AsyncClient(
            transport=httpx.MockTransport(handle), cookies=stateless_cookies(),
        )
        with patch.object(startup_service, "_client", upstream), patch.object(
            startup_service, "_configuration", return_value=(
                "https://database.example.invalid", "test-service-key",
            )
        ):
            with TestClient(app) as http:
                headers = {"Authorization": "Bearer test-user-jwt"}
                response = http.get("/api/conversations", headers=headers)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json(), {"conversations": [{"id": "chat", "title": "Test"}]})
                response = http.get("/api/integrations", headers=headers)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json(), {"integrations": [{"provider": "gemini", "api_key_last4": "1234"}]})
        self.assertEqual(len(requests), 4)
        for index in (0, 2):
            self.assertEqual(requests[index].headers["authorization"], "Bearer test-user-jwt")
        for index in (1, 3):
            self.assertEqual(requests[index].url.params["user_id"], "eq.owner")
            self.assertEqual(requests[index].headers["authorization"], "Bearer test-service-key")

    def test_lifespan_closes_all_owned_clients_even_after_provider_cleanup_failure(self):
        from app.providers import clients
        with patch.object(clients, "close_provider_clients", side_effect=RuntimeError("cleanup failed")), patch(
            "app.main.close_supabase_client"
        ) as close_database:
            with self.assertRaisesRegex(RuntimeError, "cleanup failed"):
                with TestClient(app):
                    pass
        close_database.assert_called_once_with()

    def test_all_original_routes_and_methods_remain(self):
        expected = {
            "/": {"get"},
            "/api/chat/send": {"post"},
            "/api/conversations": {"get"},
            "/api/conversations/{conversation_id}": {"get", "delete"},
            "/api/files/upload": {"post"},
            "/api/integrations": {"get", "post"},
            "/api/integrations/{provider}": {"delete"},
        }
        self.assertEqual({path: set(methods) for path, methods in app.openapi()["paths"].items()}, expected)

    def test_unauthenticated_requests_rejected_before_database_access(self):
        with patch.object(auth_service, "require_supabase") as client:
            with TestClient(app) as http:
                for path in ("/api/conversations", "/api/integrations"):
                    response = http.get(path)
                    self.assertEqual(response.status_code, 401)
                    self.assertEqual(response.json()["detail"], "Sign in with Google to use Philomath.")
            client.assert_not_called()

    def test_supabase_validation_still_required_for_every_authenticated_request(self):
        client = Mock()
        client.auth.get_user.return_value = SimpleNamespace(user=SimpleNamespace(id="owner", email="test@example.invalid"))
        with patch.object(auth_service, "require_supabase", return_value=client):
            for _ in range(2):
                user = auth_service.get_current_user("Bearer test-jwt")
                self.assertEqual(user.id, "owner")
        self.assertEqual(client.auth.get_user.call_count, 2)
        client.auth.get_user.assert_called_with("test-jwt")

    def test_invalid_auth_stays_401_but_upstream_failure_is_503(self):
        from fastapi import HTTPException
        from supabase_auth.errors import AuthApiError
        with self.assertRaises(HTTPException) as caught:
            auth_service.get_current_user("Basic test-jwt")
        self.assertEqual(caught.exception.status_code, 401)
        client = Mock()
        client.auth.get_user.side_effect = TimeoutError("sensitive upstream error")
        with patch.object(auth_service, "require_supabase", return_value=client):
            with self.assertRaises(HTTPException) as caught:
                auth_service.get_current_user("Bearer test-jwt")
        self.assertEqual(caught.exception.status_code, 503)
        self.assertNotIn("sensitive", caught.exception.detail)
        client.auth.get_user.side_effect = AuthApiError("invalid token", 403, "bad_jwt")
        with patch.object(auth_service, "require_supabase", return_value=client):
            with self.assertRaises(HTTPException) as caught:
                auth_service.get_current_user("Bearer test-jwt")
        self.assertEqual(caught.exception.status_code, 401)
