"""Read-only staging connection diagnostic; no DSN/error text is printed.

Run from backend: python -B -m tests.postgres.diagnose
Uses existing PHILOMATH_STAGING_* bindings and the existing target guards.
"""
import errno
import importlib
import json
import os
import re
import socket
import ssl
import struct
from urllib.error import HTTPError, URLError
from urllib.request import HTTPSHandler, Request, build_opener

from tests.postgres import package, staging


TIMEOUT = 5
RUNNER_OPTIONS = "-c session_replication_role=origin -c statement_timeout=20000 -c lock_timeout=2000"


class ProbeFailure(Exception):
    """Only fixed reason identifiers, never upstream messages."""


def classify(error):
    """Inspect privately; emit only fixed categories, numeric codes and SQLSTATE.

    An unclassified connection close is deliberately not labeled a bad password.
    """
    if isinstance(error, URLError) and isinstance(error.reason, BaseException):
        error = error.reason
    state = getattr(error, "sqlstate", None)
    state = state if isinstance(state, str) and re.fullmatch(r"[0-9A-Z]{5}", state) else None
    message = str(error).lower()  # Never returned, logged or embedded in reports.
    result = {"category": "unclassified", "reason": "insufficient_safe_evidence", "sqlstate": state}
    if isinstance(error, ssl.SSLCertVerificationError):
        result.update(category="tls", reason="certificate_verification_failed")
        if type(getattr(error, "verify_code", None)) is int:
            result["certificate_verify_code"] = error.verify_code
    elif isinstance(error, ssl.SSLError) or any(s in message for s in (
        "certificate verify failed", "certificate verification failed", "ssl error", "tls error",
        "root certificate file", "ssl_min_protocol_version", "certificate does not match")):
        result.update(category="tls", reason="tls_or_ca_configuration_failed")
    elif "tenant or user not found" in message:
        result.update(category="pooler_routing", reason="project_or_pooler_user_rejected")
    elif (state and state.startswith("28")) or any(s in message for s in (
        "password authentication failed", "sasl authentication failed", "no password supplied",
        "no pg_hba.conf entry", "authentication failed")):
        result.update(category="authentication", reason="database_authentication_or_access_rejected")
    elif any(s in message for s in ("unsupported startup parameter", "unsupported connection parameter",
        "unrecognized configuration parameter", "permission denied to set parameter")):
        result.update(category="startup_configuration", reason="startup_parameter_rejected")
    elif "invalid connection option" in message:
        result.update(category="libpq", reason="client_connection_option_unsupported")
    elif isinstance(error, socket.gaierror) or any(s in message for s in (
        "could not translate host name", "name or service not known", "failure in name resolution")):
        result.update(category="network", reason="dns_resolution_failed")
    elif isinstance(error, (TimeoutError, socket.timeout)) or any(s in message for s in (
        "connection timeout", "timed out", "timeout expired")):
        result.update(category="network", reason="connection_or_handshake_timeout")
    elif isinstance(error, ConnectionRefusedError) or "connection refused" in message:
        result.update(category="network", reason="connection_refused")
    elif getattr(error, "errno", None) in (errno.ENETUNREACH, errno.EHOSTUNREACH) or "network is unreachable" in message:
        result.update(category="network", reason="network_unreachable")
    elif isinstance(error, ConnectionResetError) or any(s in message for s in (
        "connection reset", "server closed the connection unexpectedly", "eof detected")):
        result.update(category="connection_closed", reason="peer_closed_connection_cause_unproven")
    elif state == "42501":
        result.update(category="authorization", reason="database_privilege_rejected")
    elif state == "53300":
        result.update(category="capacity", reason="connection_limit_exceeded")
    elif state in ("57P01", "57P03"):
        result.update(category="availability", reason="database_unavailable_or_shutting_down")
    return result


def verify_api(context):
    opener = build_opener(staging.NoRedirect(), HTTPSHandler(context=context))
    for name, path in (("PHILOMATH_STAGING_PUBLIC_KEY", "/auth/v1/settings"),
                       ("PHILOMATH_STAGING_SERVICE_ROLE_KEY", "/rest/v1/")):
        key = os.environ.get(name)
        if not key:
            raise ProbeFailure("missing_api_binding")
        request = Request("https://" + package.STAGING_REF + ".supabase.co" + path,
            headers={"apikey": key, "Authorization": "Bearer " + key})
        with opener.open(request, timeout=TIMEOUT) as response:
            if response.status != 200:
                raise ProbeFailure("api_credential_rejected")
            if response.headers.get("sb-project-ref", package.STAGING_REF) != package.STAGING_REF:
                raise ProbeFailure("api_project_mismatch")


def run():
    checks = []
    report = {"project": package.STAGING_REF, "status": "FAILED", "checks": checks,
        "writes_attempted": False, "raw_errors_printed": False}
    phase = "target_guard"
    try:
        staging.reject_ambient_libpq_settings()
        target = staging.target_from_uri(os.environ.get("PHILOMATH_STAGING_DATABASE_URL", ""))
        checks.append({"phase": phase, "status": "PASS"})
        phase = "ca_bundle"
        certifi = importlib.import_module("certifi")
        ca_file = certifi.where()
        context = ssl.create_default_context(cafile=ca_file)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        checks.append({"phase": phase, "status": "PASS", "source": "certifi"})
        phase = "api_tls_and_credentials"
        verify_api(context)
        checks.append({"phase": phase, "status": "PASS"})
        phase = "tcp_connect"
        with socket.create_connection((target.host, 5432), timeout=TIMEOUT) as raw:
            checks.append({"phase": phase, "status": "PASS"})
            phase = "postgres_ssl_request"
            raw.sendall(struct.pack("!II", 8, 80877103))
            if raw.recv(1) != b"S":
                raise ProbeFailure("postgres_tls_negotiation_rejected")
            checks.append({"phase": phase, "status": "PASS"})
            phase = "postgres_tls"
            with context.wrap_socket(raw, server_hostname=target.host) as secure:
                checks.append({"phase": phase, "status": "PASS", "tls_version": secure.version()})

        phase = "libpq_load"
        driver = importlib.import_module("psycopg")
        runtime = driver.pq.version()
        implementation = driver.pq.__impl__
        checks.append({"phase": phase, "status": "PASS",
            "runtime_libpq_version": runtime if type(runtime) is int else None,
            "implementation": implementation if implementation in ("binary", "c", "python") else "unknown"})
        parameters = dict(host=target.host, port=5432, user=target.user, password=target.password,
            dbname="postgres", sslmode="verify-full", sslrootcert=ca_file,
            connect_timeout=TIMEOUT, autocommit=True, application_name="philomath_batch4_diagnostic")
        default_ca = ssl.get_default_verify_paths().cafile
        profiles = (("libpq_minimal", {}),
            ("libpq_runner_startup_options", {"options": RUNNER_OPTIONS}),
            ("libpq_existing_runner_default_ca", {"options": RUNNER_OPTIONS, "sslrootcert": default_ca}))
        for profile, extra in profiles:
            phase = profile + "_connection"
            with driver.connect(**(parameters | extra)) as conn:
                if not conn.pgconn.ssl_in_use:
                    raise ProbeFailure("libpq_did_not_establish_tls")
                checks.append({"phase": phase, "status": "PASS"})
                phase = profile + "_readonly_identity"
                with conn.transaction():
                    conn.execute("SET TRANSACTION READ ONLY")
                    row = conn.execute("SELECT current_database(),current_user,current_setting('transaction_read_only')").fetchone()
                    if row != ("postgres", "postgres", "on"):
                        raise ProbeFailure("unexpected_database_identity_or_readonly_state")
                checks.append({"phase": phase, "status": "PASS", "read_only_identity": True})
        report["status"] = "PASS"
    except Exception as error:
        if isinstance(error, ProbeFailure):
            failure = {"category": "guard_or_protocol", "reason": error.args[0]}
        elif phase == "target_guard":
            failure = {"category": "safety_guard", "reason": "target_or_ambient_settings_rejected"}
        elif phase == "ca_bundle":
            failure = {"category": "dependency" if isinstance(error, ImportError) else "tls",
                "reason": "certifi_unavailable" if isinstance(error, ImportError) else "ca_context_initialization_failed"}
        elif phase == "libpq_load":
            message = str(error).lower()
            failure = {"category": "libpq", "reason": "client_binary_architecture_mismatch"
                if "architecture" in message or "mach-o" in message else "driver_or_client_library_load_failed"}
        elif isinstance(error, HTTPError):
            failure = {"category": "api_authentication" if error.code in (401, 403) else "api_http",
                "reason": "staging_api_request_rejected", "http_status": error.code}
            error.close()
        else:
            failure = classify(error)
        checks.append({"phase": phase, "status": "FAIL", **failure})
    return report


def main():
    result = run()
    print(json.dumps(result))
    return 0 if result["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
