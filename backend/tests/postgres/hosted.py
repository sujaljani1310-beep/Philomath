"""Explicit future hosted verification, never imported as an offline test.

Requires verified staging PostgreSQL access and already-applied hardening.
Creates only tagged synthetic Auth users/rows; never retries ambiguous writes.
"""
from concurrent.futures import ThreadPoolExecutor
import json
import os
import secrets
import ssl
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import build_opener, HTTPSHandler, Request
import uuid

from . import package, staging


class API:
    def request(self, method, path, key, token=None, body=None, prefer=None):
        allowed = ("/auth/v1/admin/users", "/auth/v1/token?grant_type=password",
                   "/rest/v1/conversations", "/rest/v1/messages", "/rest/v1/user_ai_integrations")
        if not any(path == p or path.startswith(p + "?") or
                   (p == "/auth/v1/admin/users" and path.startswith(p + "/")) for p in allowed):
            raise ValueError("Unapproved staging API path")
        if package.PRODUCTION_REF in path:
            raise ValueError("Forbidden project reference")
        headers = {"apikey": key, "Authorization": "Bearer " + (token or key),
            "Content-Type": "application/json"}
        if prefer:
            headers["Prefer"] = prefer
        request = Request("https://" + package.STAGING_REF + ".supabase.co" + path,
            data=None if body is None else json.dumps(body).encode(), headers=headers, method=method)
        opener = build_opener(staging.NoRedirect(), HTTPSHandler(context=ssl.create_default_context()))
        try:
            response = opener.open(request, timeout=12)
        except HTTPError as error:
            response = error
        with response:
            if 300 <= response.code < 400:
                raise ValueError("Credential redirect rejected")
            raw = response.read(1024 * 1024 + 1)
            if len(raw) > 1024 * 1024:
                raise ValueError("Oversized verification response")
            return response.code, json.loads(raw) if raw else None


def main():
    run = uuid.uuid4().hex
    api = API()
    known_users = []
    checks = []
    cleanup_errors = 0
    success = False
    try:
        staging.reject_ambient_libpq_settings()
        target = staging.target_from_uri(os.environ.get("PHILOMATH_STAGING_DATABASE_URL", ""))
        staging.verify_api()
        import psycopg
        from cryptography.fernet import Fernet
        with psycopg.connect(host=target.host, port=5432, user=target.user, password=target.password,
            dbname="postgres", sslmode="verify-full", sslrootcert=ssl.get_default_verify_paths().cafile,
            connect_timeout=8, autocommit=True,
            options="-c session_replication_role=origin -c statement_timeout=5000 -c lock_timeout=2000") as conn:
            if not conn.pgconn.ssl_in_use:
                raise ValueError("TLS required")
            with conn.transaction():
                conn.execute("SET TRANSACTION READ ONLY")
                conn.execute("SET LOCAL statement_timeout='5s'")
                conn.execute("SELECT set_config('philomath.batch4_target',%s,true)", (package.STAGING_REF,))
                conn.execute(package.sql("target_guard.sql") + package.sql("catalog_contract.sql"))
            service = os.environ["PHILOMATH_STAGING_SERVICE_ROLE_KEY"]
            public = os.environ["PHILOMATH_STAGING_PUBLIC_KEY"]

            def expect(method, path, expected, key=service, token=None, body=None, prefer=None):
                code, data = api.request(method, path, key, token, body, prefer)
                if code not in expected:
                    raise AssertionError("Unexpected staging HTTP status " + str(code))
                return data

            for suffix in ("a", "b"):
                email = "batch4-" + run + "-" + suffix + "@example.invalid"
                password = secrets.token_urlsafe(32)
                user = expect("POST", "/auth/v1/admin/users", (200, 201), body={
                    "email": email, "password": password, "email_confirm": True,
                    "user_metadata": {"philomath_batch4_run": run}})
                user = user.get("user", user)
                user_id = str(uuid.UUID(user["id"]))
                known_users.append(user_id)
                login = expect("POST", "/auth/v1/token?grant_type=password", (200,), key=public,
                    body={"email": email, "password": password})
                user["token"] = login["access_token"]
                user["conversation"] = "batch4_" + run + "_" + suffix
                if suffix == "a":
                    a = user
                else:
                    b = user
                expect("POST", "/rest/v1/conversations", (201,), body={
                    "id": user["conversation"], "user_id": user_id, "title": "Synthetic Batch 4", "mode": "basic"})
                expect("POST", "/rest/v1/messages", (201,), body={"conversation_id": user["conversation"],
                    "user_id": user_id, "role": "user", "content": "Synthetic Batch 4"})
            checks.append("two synthetic Auth users and backend-owned fixtures")

            for user, other in ((a, b), (b, a)):
                for table, field in (("conversations", "id"), ("messages", "conversation_id")):
                    for parent, count in ((user, 1), (other, 0)):
                        path = "/rest/v1/" + table + "?" + urlencode({field: "eq." + parent["conversation"], "select": "id"})
                        rows = expect("GET", path, (200,), key=public, token=user["token"])
                        if len(rows) != count:
                            raise AssertionError("Hosted RLS isolation failed")
                checks.append("symmetric own reads and cross-user RLS")
                for method, body in (("POST", {"id": "forbidden_" + run, "user_id": user["id"]}),
                                     ("PATCH", {"title": "forbidden"}), ("DELETE", None)):
                    path = "/rest/v1/conversations" + ("" if method == "POST" else "?" + urlencode({"id": "eq." + user["conversation"]}))
                    expect(method, path, (403,), key=public, token=user["token"], body=body)
                for method, body in (("POST", {"conversation_id": user["conversation"],
                    "user_id": user["id"], "role": "user", "content": "forbidden"}),
                    ("PATCH", {"content": "forbidden"}), ("DELETE", None)):
                    path = "/rest/v1/messages" + ("" if method == "POST" else "?" +
                        urlencode({"conversation_id": "eq." + user["conversation"]}))
                    expect(method, path, (403,), key=public, token=user["token"], body=body)
                checks.append("browser writes denied")
                expect("GET", "/rest/v1/user_ai_integrations?select=id", (403,), key=public, token=user["token"])
            for table in package.TABLES:
                expect("GET", "/rest/v1/" + table + "?select=id&limit=0", (401, 403), key=public)
            checks.append("anonymous and integration access denied")
            rejected = expect("POST", "/rest/v1/messages", (400, 409), body={"conversation_id": b["conversation"],
                "user_id": a["id"], "role": "user", "content": "Forbidden synthetic owner"})
            if rejected.get("code") not in ("23503", "23514"):
                raise AssertionError("Expected an ownership constraint rejection")
            checks.append("service-role mismatched parent rejected")
            cipher = Fernet(Fernet.generate_key()).encrypt(b"synthetic-never-real-api-key").decode()
            payload = {"user_id": a["id"], "provider": "gemini", "display_name": "Gemini",
                "encrypted_api_key": cipher, "api_key_last4": "1234"}
            def upsert(_):
                return expect("POST", "/rest/v1/user_ai_integrations?on_conflict=user_id,provider&select=id",
                    (200, 201), body=payload, prefer="resolution=merge-duplicates,return=representation")[0]["id"]
            with ThreadPoolExecutor(max_workers=2) as pool:
                ids = list(pool.map(upsert, range(2)))
            if ids[0] != ids[1]:
                raise AssertionError("Integration upsert identity differs")
            checks.append("concurrent integration upserts retain one ID")
            for user, other in ((a, b), (b, a)):
                with conn.transaction():
                    conn.execute("SET TRANSACTION READ ONLY")
                    conn.execute("SET LOCAL statement_timeout='5s'")
                    conn.execute("SET LOCAL ROLE authenticated")
                    conn.execute("SELECT set_config('request.jwt.claim.sub',%s,true)", (user["id"],))
                    if conn.execute("SELECT count(*) FROM public.conversations WHERE id=%s", (user["conversation"],)).fetchone()[0] != 1:
                        raise AssertionError("Direct SQL own read failed")
                    if conn.execute("SELECT count(*) FROM public.conversations WHERE id=%s", (other["conversation"],)).fetchone()[0] != 0:
                        raise AssertionError("Direct SQL foreign read was exposed")
            checks.append("direct SQL authenticated reads")
            with conn.transaction(force_rollback=True):
                conn.execute("SET LOCAL statement_timeout='5s'")
                for name, value in (("user_a", a["id"]), ("user_b", b["id"]),
                    ("parent_a", a["conversation"]), ("parent_b", b["conversation"]),
                    ("target", package.STAGING_REF)):
                    conn.execute("SELECT set_config(%s,%s,true)", ("philomath.batch4_" + name, value))
                conn.execute(package.sql("target_guard.sql") + package.sql("two_user_permissions.sql"))
            checks.append("direct SQL writes, anonymous reads and dangerous grants denied")
            expect("DELETE", "/rest/v1/conversations?" + urlencode({"id": "eq." + b["conversation"]}), (204,))
            rows = expect("GET", "/rest/v1/messages?" + urlencode({"conversation_id": "eq." + b["conversation"], "select": "id"}), (200,))
            if rows:
                raise AssertionError("Cascade did not remove synthetic messages")
            checks.append("valid deletion cascade")
            success = True
    except Exception as error:
        print(json.dumps({"status": "FAILED", "run": run, "error_type": type(error).__name__,
            "passed_checks": checks, "retry_performed": False}))
    finally:
        # Only confirmed synthetic IDs created in this invocation are eligible.
        # Unknown outcomes are not retried or searched across existing users.
        service = os.environ.get("PHILOMATH_STAGING_SERVICE_ROLE_KEY")
        for user_id in known_users:
            for path in ("/rest/v1/user_ai_integrations?" + urlencode({"user_id": "eq." + user_id}),
                         "/rest/v1/conversations?" + urlencode({"user_id": "eq." + user_id}),
                         "/auth/v1/admin/users/" + user_id):
                try:
                    code, _ = api.request("DELETE", path, service)
                    if code not in (200, 204):
                        cleanup_errors += 1
                except Exception:
                    cleanup_errors += 1
        print(json.dumps({"project": package.STAGING_REF, "run": run,
            "status": "PASSED" if success and not cleanup_errors else "NEEDS REVIEW",
            "passed_checks": checks, "cleanup_errors": cleanup_errors,
            "unknown_create_outcomes_require_manual_review": not success}))
    return 0 if success and not cleanup_errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
