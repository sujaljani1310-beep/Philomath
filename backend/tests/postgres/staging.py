"""Explicit, TLS-only staging runner. No REST SQL endpoint and no write retry."""
import argparse
from dataclasses import dataclass, field
import json
import os
import re
import ssl
from urllib.parse import parse_qsl, unquote, urlsplit
from urllib.request import build_opener, HTTPSHandler, HTTPRedirectHandler, Request

from . import package


@dataclass(frozen=True)
class Target:
    host: str
    user: str
    password: str = field(repr=False)


def reject_ambient_libpq_settings():
    # libpq can otherwise use PGSERVICE/PGHOSTADDR/PGOPTIONS despite an explicit
    # host URI. Verification tools accept only the dedicated staging binding.
    if any(value for name, value in os.environ.items() if name.startswith("PG")):
        raise ValueError("Ambient libpq settings require review")


def target_from_uri(value):
    try:
        reject_ambient_libpq_settings()
        uri = urlsplit(value)
        user = unquote(uri.username or "")
        if package.PRODUCTION_REF in unquote(value):
            raise ValueError()
        pooler = bool(uri.hostname and re.fullmatch(r"[a-zA-Z0-9.-]+\.pooler\.supabase\.com", uri.hostname)
            and user == "postgres." + package.STAGING_REF)
        direct = uri.hostname == "db." + package.STAGING_REF + ".supabase.co" and user == "postgres"
        query = parse_qsl(uri.query, strict_parsing=True)
        if (uri.scheme not in ("postgres", "postgresql") or not (pooler or direct)
            or (uri.port or 5432) != 5432 or uri.path != "/postgres" or uri.fragment
            or not uri.password or value != value.strip()
            or any(k not in ("sslmode", "application_name") for k, _ in query)
            or any(k == "sslmode" and v not in ("require", "verify-ca", "verify-full") for k, v in query)
            or len(dict(query)) != len(query)
            or re.search(r"%(?![0-9a-fA-F]{2})", uri.password)
            or any(c in unquote(value) for c in "\r\n\x00")):
            raise ValueError()
        return Target(uri.hostname, user, unquote(uri.password))
    except (ValueError, TypeError):
        raise ValueError("Unapproved or malformed staging database target") from None


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError("Staging credential redirects are forbidden")


def verify_api():
    opener = build_opener(NoRedirect(), HTTPSHandler(context=ssl.create_default_context()))
    for name, path in (("PHILOMATH_STAGING_PUBLIC_KEY", "/auth/v1/settings"),
                       ("PHILOMATH_STAGING_SERVICE_ROLE_KEY", "/rest/v1/")):
        key = os.environ.get(name)
        if not key:
            raise ValueError("Missing staging API binding")
        request = Request("https://" + package.STAGING_REF + ".supabase.co" + path,
            headers={"apikey": key, "Authorization": "Bearer " + key})
        with opener.open(request, timeout=10) as response:
            if response.status != 200:
                raise ValueError("Staging API credential rejected")
            if response.headers.get("sb-project-ref", package.STAGING_REF) != package.STAGING_REF:
                raise ValueError("Unexpected staging API identity")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--action", choices=("inspect", "baseline", "harden"), default="inspect")
    args = parser.parse_args(argv)
    attempted_write = False
    try:
        target = target_from_uri(os.environ.get("PHILOMATH_STAGING_DATABASE_URL", ""))
        verify_api()
        import psycopg  # Optional verification tooling, not an application dependency.
        with psycopg.connect(host=target.host, port=5432, user=target.user, password=target.password,
            dbname="postgres", sslmode="verify-full", sslrootcert=ssl.get_default_verify_paths().cafile,
            connect_timeout=8, autocommit=True, application_name="philomath_batch4_verified",
            options="-c session_replication_role=origin -c statement_timeout=20000 -c lock_timeout=2000") as conn:
            if not conn.pgconn.ssl_in_use:
                raise ValueError("TLS was not established")
            with conn.transaction():
                conn.execute("SET TRANSACTION READ ONLY")
                if conn.execute("SELECT current_database(),current_user").fetchone() != ("postgres", "postgres"):
                    raise ValueError("Unexpected database role/name")
                snapshot = conn.execute(package.SNAPSHOT).fetchone()[0]
                if args.action == "inspect":
                    print(json.dumps({"project": package.STAGING_REF, "tls_verified": True,
                        "catalog_counts": {k: len(v or []) for k, v in snapshot.items()},
                        "hosted_auth_and_rls_tests": "NOT RUN"}))
                    return 0
                if args.action == "baseline":
                    # Any partial/existing application schema is a blocker, not
                    # permission to overwrite it or invent a missing table.
                    if any(snapshot.values()) or conn.execute("SELECT EXISTS (SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace WHERE n.nspname='public' AND p.proname='set_philomath_updated_at')").fetchone()[0]:
                        raise ValueError("Baseline requires absent application objects")
                else:
                    package.verify_baseline(snapshot)
            # Revalidate the local target and API immediately before each changing
            # transaction; never retry its SQL or commit after an ambiguous error.
            if target_from_uri(os.environ["PHILOMATH_STAGING_DATABASE_URL"]) != target:
                raise ValueError("Staging target changed")
            verify_api()
            with conn.transaction():
                conn.execute("SELECT set_config('philomath.batch4_target',%s,true)", (package.STAGING_REF,))
                conn.execute("SET LOCAL lock_timeout='2s'")
                if args.action == "harden":
                    conn.execute("LOCK TABLE public.conversations,public.messages,public.user_ai_integrations IN ACCESS EXCLUSIVE MODE")
                    package.verify_baseline(conn.execute(package.SNAPSHOT).fetchone()[0])
                attempted_write = True
                conn.execute(package.baseline() if args.action == "baseline" else package.hardening())
            print(json.dumps({"project": package.STAGING_REF, "action": args.action,
                "commit_acknowledged": True, "hosted_auth_and_rls_tests": "NOT RUN"}))
            return 0
    except Exception as exc:
        # No URLs, server error bodies, keys, passwords or tracebacks in reports.
        print(json.dumps({"status": "BLOCKED", "error_type": type(exc).__name__,
            "write_outcome": "unconfirmed; inspect before any rerun" if attempted_write else "no write attempted",
            "retry_performed": False}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
