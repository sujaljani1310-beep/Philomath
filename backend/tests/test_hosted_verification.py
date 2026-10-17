"""Mock-only verification of future hosted runner; no credentials or network."""
from contextlib import redirect_stdout
import io
import json
import os
from unittest.mock import MagicMock, patch
import unittest
from urllib.parse import parse_qs, urlsplit

from tests.postgres import hosted, package
from tests.test_postgres_package import URI


class HostedPackageTests(unittest.TestCase):
    def test_unapproved_paths_never_send_credentials(self):
        with patch.object(hosted, "build_opener") as opener:
            for path in ("https://evil.invalid", "/rpc/execute_sql", "/rest/v1/messages/../../rpc/sql"):
                with self.assertRaises(ValueError):
                    hosted.API().request("POST", path, "synthetic-key")
        opener.assert_not_called()

    def test_oversized_response_is_bounded(self):
        response = MagicMock()
        response.__enter__.return_value = response
        response.code = 200
        response.read.return_value = b"x" * (1024 * 1024 + 1)
        with patch.object(hosted, "build_opener") as opener:
            opener.return_value.open.return_value = response
            with self.assertRaises(ValueError):
                hosted.API().request("GET", "/rest/v1/messages", "synthetic-key")
        response.read.assert_called_once_with(1024 * 1024 + 1)

    def test_invalid_target_creates_no_auth_users_or_rows(self):
        with patch.dict(os.environ, {"PHILOMATH_STAGING_DATABASE_URL": URI.replace(package.STAGING_REF, package.PRODUCTION_REF)}), patch.object(hosted.API, "request") as api, redirect_stdout(io.StringIO()):
            self.assertEqual(hosted.main(), 1)
        api.assert_not_called()

    def test_complete_future_flow_uses_only_new_synthetic_ids(self):
        users = ("11111111-1111-4111-8111-111111111111", "22222222-2222-4222-8222-222222222222")
        created = []
        conversations = {}
        deleted_auth = []
        def request(method, path, key, token=None, body=None, prefer=None):
            if path == "/auth/v1/admin/users" and method == "POST":
                user = users[len(created)]; created.append(user)
                self.assertTrue(body["email"].endswith("@example.invalid"))
                self.assertIn("philomath_batch4_run", body["user_metadata"])
                return 200, {"id": user}
            if path.startswith("/auth/v1/token"):
                return 200, {"access_token": "token-" + created[-1]}
            if method == "DELETE" and path.startswith("/auth/v1/admin/users/"):
                deleted_auth.append(path.rsplit("/", 1)[1]); return 200, {}
            if key == "public":
                if method != "GET" or "integrations" in path or "limit=0" in path:
                    return 403, {}
                fields = parse_qs(urlsplit(path).query)
                parent = next(v[0][3:] for k,v in fields.items() if k in ("id", "conversation_id"))
                owner = token.removeprefix("token-")
                return 200, [{"id": "synthetic"}] if conversations.get(parent) == owner else []
            if method == "POST" and path == "/rest/v1/conversations":
                conversations[body["id"]] = body["user_id"]; return 201, None
            if method == "POST" and path == "/rest/v1/messages":
                return (409, {"code": "23503"}) if body["content"].startswith("Forbidden") else (201, None)
            if method == "POST" and "user_ai_integrations" in path:
                self.assertNotIn("synthetic-never-real-api-key", body["encrypted_api_key"])
                return 201, [{"id": "stable-synthetic-id"}]
            return (204, None) if method == "DELETE" else (200, [])
        conn = MagicMock()
        conn.execute.return_value.fetchone.side_effect = [(1,), (0,), (1,), (0,)]
        driver = MagicMock()
        driver.connect.return_value.__enter__.return_value = conn
        output = io.StringIO()
        with patch.dict(os.environ, {"PHILOMATH_STAGING_DATABASE_URL": URI,
            "PHILOMATH_STAGING_SERVICE_ROLE_KEY": "service", "PHILOMATH_STAGING_PUBLIC_KEY": "public"}), patch.dict("sys.modules", {"psycopg": driver}), patch.object(hosted.staging, "verify_api"), patch.object(hosted.API, "request", side_effect=request), redirect_stdout(output):
            self.assertEqual(hosted.main(), 0)
        self.assertEqual(deleted_auth, list(users))
        self.assertEqual(json.loads(output.getvalue())["status"], "PASSED")
        self.assertEqual(driver.connect.call_args.kwargs["sslmode"], "verify-full")
