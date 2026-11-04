import importlib.util
import os
import sys
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from types import SimpleNamespace
from unittest.mock import Mock, patch

import httpx

from app.services import supabase_service as service


class SupabaseServiceTests(unittest.TestCase):
    def setUp(self):
        self.settings = SimpleNamespace(
            database_timeout_seconds=15,
            auth_timeout_seconds=10,
            connect_timeout_seconds=5,
        )
        # All values are local fixtures. Suppress dotenv loading and clear the
        # inherited environment so no test reads credentials or contacts a host.
        self.patches = [
            patch.object(service, "get_settings", return_value=self.settings),
            patch.object(service, "supabase", None),
            patch.object(service, "_http_client", None),
            patch.dict(os.environ, {}, clear=True),
        ]
        for active_patch in self.patches:
            active_patch.start()
        self.addCleanup(self.stop_patches)

    def stop_patches(self):
        service.close_supabase_client()
        for active_patch in reversed(self.patches):
            active_patch.stop()

    def configure(self):
        os.environ["SUPABASE_URL"] = "https://supabase.example.invalid"
        os.environ["SUPABASE_SERVICE_ROLE_KEY"] = "test-service-role-key"

    def test_import_does_not_import_supabase_sdk_or_read_configuration(self):
        spec = importlib.util.spec_from_file_location(
            "supabase_service_lazy_import_test", Path(service.__file__)
        )
        module = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, {"supabase": None}), patch(
            "app.settings.get_settings", side_effect=AssertionError("eager settings")
        ):
            spec.loader.exec_module(module)
        self.assertIsNone(module.supabase)
        self.assertIsNone(module._http_client)

    def test_missing_configuration_retains_error_and_does_not_create_client(self):
        with patch.object(service, "_create_supabase_client") as create:
            self.assertFalse(service.is_supabase_connected())
            with self.assertRaisesRegex(RuntimeError, "Supabase is not configured"):
                service.require_supabase()
        create.assert_not_called()

    def test_configuration_check_is_not_a_connection_probe(self):
        self.configure()
        with patch.object(service, "_create_supabase_client") as create:
            self.assertTrue(service.is_supabase_connected())
            self.assertIsNone(service.supabase)
        create.assert_not_called()

    def test_service_role_key_preferred_and_fallback_key_supported(self):
        self.configure()
        os.environ["SUPABASE_KEY"] = "test-fallback-key"
        client, http_client = Mock(), Mock()
        with patch.object(
            service, "_create_supabase_client", return_value=(client, http_client)
        ) as create:
            self.assertIs(service.require_supabase(), client)
            create.assert_called_once_with(
                "https://supabase.example.invalid", "test-service-role-key"
            )
            service.close_supabase_client()
            del os.environ["SUPABASE_SERVICE_ROLE_KEY"]
            self.assertIs(service.require_supabase(), client)
            self.assertEqual(create.call_args.args[1], "test-fallback-key")

    def test_concurrent_first_requests_initialize_once_and_reuse_client(self):
        self.configure()
        client, http_client = Mock(), Mock()
        barrier = Barrier(12)

        def require_after_barrier(_):
            barrier.wait(timeout=5)
            return service.require_supabase()

        with patch.object(
            service, "_create_supabase_client", return_value=(client, http_client)
        ) as create, ThreadPoolExecutor(max_workers=12) as pool:
            results = list(pool.map(require_after_barrier, range(12)))
        self.assertTrue(all(result is client for result in results))
        create.assert_called_once()
        self.assertIs(service.require_supabase(), client)

    def test_close_is_idempotent_and_allows_new_lifecycle(self):
        self.configure()
        first_client, second_client = Mock(), Mock()
        first_http, second_http = Mock(), Mock()
        with patch.object(
            service,
            "_create_supabase_client",
            side_effect=[(first_client, first_http), (second_client, second_http)],
        ):
            self.assertIs(service.require_supabase(), first_client)
            service.close_supabase_client()
            service.close_supabase_client()
            first_http.close.assert_called_once_with()
            self.assertIsNone(service.supabase)
            self.assertIs(service.require_supabase(), second_client)
            service.close_supabase_client()
            second_http.close.assert_called_once_with()

    def test_sdk_uses_one_pool_with_distinct_auth_and_database_timeouts(self):
        # Import the installed SDK before patching HTTPX's constructor. All SDK
        # requests are handled by MockTransport, never by a network transport.
        import supabase  # noqa: F401

        self.configure()
        requests = []
        created_clients = []
        client_class = httpx.Client

        def handle_request(request):
            requests.append(request)
            if request.url.path == "/auth/v1/user":
                return httpx.Response(200, json={
                    "id": "test-user",
                    "aud": "authenticated",
                    "app_metadata": {},
                    "user_metadata": {},
                    "created_at": "2026-01-01T00:00:00Z",
                })
            return httpx.Response(200, json=[])

        def create_http_client(**kwargs):
            kwargs["transport"] = httpx.MockTransport(handle_request)
            client = client_class(**kwargs)
            created_clients.append(client)
            return client

        with patch("httpx.Client", side_effect=create_http_client):
            client = service.require_supabase()
            self.assertEqual(requests, [], "initialization must not probe Supabase")
            self.assertEqual(client.auth.get_user("test-user-jwt").user.id, "test-user")
            self.assertEqual(client.table("conversations").select("id").execute().data, [])
            self.assertIs(service.require_supabase(), client)

        self.assertEqual(len(created_clients), 1)
        self.assertIs(client.auth._http_client, created_clients[0])
        self.assertIs(client.postgrest.session, created_clients[0])
        self.assertEqual(requests[0].extensions["timeout"], {
            "connect": 5, "read": 10, "write": 10, "pool": 5,
        })
        self.assertEqual(requests[1].extensions["timeout"], {
            "connect": 5, "read": 15, "write": 15, "pool": 5,
        })
        self.assertEqual(requests[0].headers["authorization"], "Bearer test-user-jwt")
        self.assertEqual(requests[1].headers["authorization"], "Bearer test-service-role-key")
        service.close_supabase_client()
        self.assertTrue(created_clients[0].is_closed)

    def test_failed_sdk_initialization_closes_pool_and_can_retry(self):
        import supabase

        self.configure()
        http_client = Mock()
        with patch("httpx.Client", return_value=http_client), patch.object(
            supabase, "create_client", side_effect=ValueError("invalid test configuration")
        ):
            with self.assertRaisesRegex(ValueError, "invalid test configuration"):
                service.require_supabase()
        http_client.close.assert_called_once_with()
        self.assertIsNone(service.supabase)
        self.assertIsNone(service._http_client)
        with patch.object(
            service, "_create_supabase_client", return_value=(Mock(), Mock())
        ) as create:
            service.require_supabase()
        create.assert_called_once()


if __name__ == "__main__":
    unittest.main()
