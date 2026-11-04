"""Installed PostgREST must not replay ambiguous database writes."""

import unittest
from unittest.mock import patch

import httpx


class DatabaseWriteRetryTests(unittest.TestCase):
    def test_insert_and_update_are_single_attempt_on_timeout_and_service_failure(self):
        from supabase import ClientOptions, create_client

        for operation in ("insert", "update", "delete"):
            for failure in ("timeout", 503, 520):
                with self.subTest(operation=operation, failure=failure):
                    seen = []
                    def handle(request):
                        seen.append(request.method)
                        if failure == "timeout":
                            raise httpx.ReadTimeout("synthetic ambiguous write", request=request)
                        return httpx.Response(failure, json={
                            "code": "synthetic", "message": "synthetic unavailable",
                            "hint": None, "details": None,
                        })
                    with httpx.Client(transport=httpx.MockTransport(handle)) as upstream:
                        sdk = create_client("https://synthetic.invalid", "synthetic-service-key", options=ClientOptions(httpx_client=upstream))
                        query = sdk.table("messages")
                        if operation == "insert":
                            query = query.insert({"content": "synthetic"})
                        elif operation == "update":
                            query = query.update({"content": "synthetic"}).eq("id", "synthetic")
                        else:
                            query = query.delete().eq("id", "synthetic")
                        with patch("postgrest._sync.request_builder.time.sleep") as sleep:
                            with self.assertRaises(Exception):
                                query.execute()
                            sleep.assert_not_called()
                        self.assertEqual(seen, [{"insert": "POST", "update": "PATCH", "delete": "DELETE"}[operation]])
