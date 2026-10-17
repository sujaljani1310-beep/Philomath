"""Synthetic-only diagnostics tests: no sockets, credentials or real databases."""
from contextlib import ExitStack, redirect_stdout
import io
import json
import os
from pathlib import Path
import socket
import ssl
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError, URLError

from tests.postgres import diagnose, package
from tests.test_postgres_package import URI
from tests.test_staging_ca import write_synthetic_ca


class UpstreamFailure(Exception):
    def __init__(self, message, state=None):
        super().__init__(message)
        self.sqlstate = state


class DiagnosticClassificationTests(unittest.TestCase):
    def test_fixed_categories_and_sqlstate_without_raw_error_text(self):
        examples = (
            (socket.gaierror(-2, "private-sentinel"), "network", "dns_resolution_failed"),
            (TimeoutError("private-sentinel"), "network", "connection_or_handshake_timeout"),
            (ConnectionRefusedError("private-sentinel"), "network", "connection_refused"),
            (ConnectionResetError("private-sentinel"), "connection_closed", "peer_closed_connection_cause_unproven"),
            (ssl.SSLCertVerificationError(1, "private-sentinel"), "tls", "certificate_verification_failed"),
            (URLError(ssl.SSLCertVerificationError(1, "private-sentinel")), "tls", "certificate_verification_failed"),
            (UpstreamFailure("private-sentinel", "28P01"), "authentication", "database_authentication_or_access_rejected"),
            (UpstreamFailure("private-sentinel", "28000"), "authentication", "database_authentication_or_access_rejected"),
            (UpstreamFailure("password authentication failed private-sentinel"), "authentication", "database_authentication_or_access_rejected"),
            (UpstreamFailure("Tenant or user not found private-sentinel", "XX000"), "pooler_routing", "project_or_pooler_user_rejected"),
            (UpstreamFailure("unsupported startup parameter private-sentinel", "XX000"), "startup_configuration", "startup_parameter_rejected"),
            (UpstreamFailure("permission denied to set parameter private-sentinel", "42501"), "startup_configuration", "startup_parameter_rejected"),
            (UpstreamFailure("invalid connection option private-sentinel"), "libpq", "client_connection_option_unsupported"),
            (UpstreamFailure("root certificate file private-sentinel does not exist"), "tls", "tls_or_ca_configuration_failed"),
            (UpstreamFailure("private-sentinel", "42501"), "authorization", "database_privilege_rejected"),
            (UpstreamFailure("private-sentinel", "53300"), "capacity", "connection_limit_exceeded"),
            (UpstreamFailure("private-sentinel", "57P03"), "availability", "database_unavailable_or_shutting_down"),
            (UpstreamFailure("private-sentinel"), "unclassified", "insufficient_safe_evidence"),
        )
        for error, category, reason in examples:
            with self.subTest(category=category, reason=reason):
                result = diagnose.classify(error)
                self.assertEqual((result["category"], result["reason"]), (category, reason))
                self.assertNotIn("private-sentinel", json.dumps(result))

    def test_invalid_sqlstate_is_not_echoed(self):
        result = diagnose.classify(UpstreamFailure("private-sentinel", "private-sentinel"))
        self.assertIsNone(result["sqlstate"])
        self.assertNotIn("private-sentinel", json.dumps(result))


class DiagnosticFlowTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.dict(os.environ, {"PHILOMATH_STAGING_DATABASE_URL": URI}, clear=True))
        self.context = MagicMock()
        self.secure = self.context.wrap_socket.return_value.__enter__.return_value
        self.secure.version.return_value = "TLSv1.3"
        self.real_make_context = ssl.create_default_context
        self.make_context = self.stack.enter_context(patch.object(diagnose.ssl, "create_default_context", return_value=self.context))
        self.stack.enter_context(patch.object(diagnose.ssl, "get_default_verify_paths", return_value=SimpleNamespace(cafile="synthetic-default-ca.pem")))
        self.api = self.stack.enter_context(patch.object(diagnose, "verify_api"))
        self.tcp = self.stack.enter_context(patch.object(diagnose.socket, "create_connection"))
        self.raw = self.tcp.return_value.__enter__.return_value
        self.raw.recv.return_value = b"S"
        self.driver = MagicMock()
        self.driver.pq.version.return_value = 170000
        self.driver.pq.__impl__ = "binary"
        self.conn = self.driver.connect.return_value.__enter__.return_value
        self.conn.pgconn.ssl_in_use = True
        self.conn.execute.return_value.fetchone.return_value = ("postgres", "postgres", "on")
        def load(name):
            return SimpleNamespace(where=lambda: "synthetic-ca.pem") if name == "certifi" else self.driver
        self.load = self.stack.enter_context(patch.object(diagnose.importlib, "import_module", side_effect=load))

    def test_success_keeps_verification_readonly_and_compares_startup_profiles(self):
        result = diagnose.run()
        self.assertEqual(result["status"], "PASS")
        self.make_context.assert_called_once_with(cafile="synthetic-ca.pem")
        self.context.wrap_socket.assert_called_once_with(self.raw, server_hostname="aws-0-test.pooler.supabase.com")
        self.raw.sendall.assert_called_once_with(b"\x00\x00\x00\x08\x04\xd2\x16/")
        calls = self.driver.connect.call_args_list
        self.assertEqual(len(calls), 3)
        self.assertNotIn("options", calls[0].kwargs)
        self.assertEqual(calls[1].kwargs["options"], diagnose.RUNNER_OPTIONS)
        for i, call in enumerate(calls):
            self.assertEqual(call.kwargs["sslmode"], "verify-full")
            self.assertEqual(call.kwargs["sslrootcert"], "synthetic-ca.pem" if i < 2 else "synthetic-default-ca.pem")
        for call in self.conn.execute.call_args_list:
            self.assertIn(call.args[0], ("SET TRANSACTION READ ONLY", "SELECT current_database(),current_user,current_setting('transaction_read_only')"))
        self.assertFalse(result["writes_attempted"])
        self.assertNotIn("synthetic-pass", json.dumps(result))

    def test_production_rejected_before_any_network_or_credential_send(self):
        os.environ["PHILOMATH_STAGING_DATABASE_URL"] = URI.replace(package.STAGING_REF, package.PRODUCTION_REF)
        result = diagnose.run()
        self.assertEqual(result["checks"][-1]["category"], "safety_guard")
        self.api.assert_not_called()
        self.tcp.assert_not_called()
        self.driver.connect.assert_not_called()

    def test_configured_ca_is_used_for_probe_and_all_libpq_profiles_only(self):
        directory = Path(self.enterContext(TemporaryDirectory()))
        ca = directory / "synthetic-ca.pem"
        write_synthetic_ca(ca)
        os.environ[diagnose.staging.POSTGRES_CA_ENV] = str(ca)
        postgres_context = MagicMock()
        postgres_context.wrap_socket.return_value = self.context.wrap_socket.return_value
        self.make_context.side_effect = [self.context, postgres_context]
        result = diagnose.run()
        self.assertEqual(result["status"], "PASS")
        self.assertEqual([c.kwargs for c in self.make_context.call_args_list], [
            {"cafile": "synthetic-ca.pem"}, {"cafile": str(ca.resolve())}])
        self.api.assert_called_once_with(self.context)
        postgres_context.wrap_socket.assert_called_once_with(self.raw,
            server_hostname="aws-0-test.pooler.supabase.com")
        for call in self.driver.connect.call_args_list:
            self.assertEqual(call.kwargs["sslrootcert"], str(ca.resolve()))
            self.assertEqual(call.kwargs["sslmode"], "verify-full")
        phases = {c["phase"] for c in result["checks"]}
        self.assertIn("libpq_configured_runner_ca_connection", phases)
        self.assertNotIn("libpq_existing_runner_default_ca_connection", phases)
        self.assertNotIn(str(ca), json.dumps(result))

    def test_configured_ca_context_keeps_certificate_and_hostname_verification(self):
        directory = Path(self.enterContext(TemporaryDirectory()))
        ca = directory / "synthetic-ca.pem"
        write_synthetic_ca(ca)
        os.environ[diagnose.staging.POSTGRES_CA_ENV] = str(ca)
        context = self.real_make_context(cafile=str(ca))
        wrap = self.enterContext(patch.object(context, "wrap_socket", return_value=self.context.wrap_socket.return_value))
        self.make_context.side_effect = [self.context, context]
        self.assertEqual(diagnose.run()["status"], "PASS")
        self.assertTrue(context.check_hostname)
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertGreaterEqual(context.minimum_version, ssl.TLSVersion.TLSv1_2)
        wrap.assert_called_once_with(self.raw, server_hostname="aws-0-test.pooler.supabase.com")

    def test_invalid_configured_ca_stops_before_api_tcp_or_libpq(self):
        os.environ[diagnose.staging.POSTGRES_CA_ENV] = "/private-sentinel/missing-ca.pem"
        result = diagnose.run()
        self.assertEqual(result["checks"][-1]["phase"], "postgres_ca_bundle")
        self.assertEqual(result["checks"][-1]["reason"], "configured_postgres_ca_invalid")
        self.api.assert_not_called()
        self.tcp.assert_not_called()
        self.driver.connect.assert_not_called()
        self.assertNotIn("private-sentinel", json.dumps(result))

    def test_custom_ca_hostname_failure_never_reaches_database_authentication(self):
        directory = Path(self.enterContext(TemporaryDirectory()))
        ca = directory / "synthetic-ca.pem"
        write_synthetic_ca(ca)
        os.environ[diagnose.staging.POSTGRES_CA_ENV] = str(ca)
        self.context.wrap_socket.side_effect = ssl.SSLCertVerificationError(1, "private-sentinel hostname mismatch")
        result = diagnose.run()
        self.assertEqual(result["checks"][-1]["phase"], "postgres_tls")
        self.assertEqual(result["checks"][-1]["category"], "tls")
        self.driver.connect.assert_not_called()
        self.tcp.return_value.__exit__.assert_called_once()
        self.assertNotIn("private-sentinel", json.dumps(result))

    def test_custom_ca_libpq_verification_failure_never_tries_default_or_require(self):
        directory = Path(self.enterContext(TemporaryDirectory()))
        ca = directory / "synthetic-ca.pem"
        write_synthetic_ca(ca)
        os.environ[diagnose.staging.POSTGRES_CA_ENV] = str(ca)
        self.driver.connect.side_effect = UpstreamFailure("certificate verify failed private-sentinel")
        result = diagnose.run()
        self.assertEqual(result["checks"][-1]["phase"], "libpq_minimal_connection")
        self.assertEqual(result["checks"][-1]["category"], "tls")
        self.driver.connect.assert_called_once()
        self.assertEqual(self.driver.connect.call_args.kwargs["sslrootcert"], str(ca.resolve()))
        self.assertEqual(self.driver.connect.call_args.kwargs["sslmode"], "verify-full")

    def test_pg_service_override_rejected(self):
        os.environ["PGSERVICE"] = "private-sentinel"
        self.assertEqual(diagnose.run()["checks"][-1]["category"], "safety_guard")
        self.tcp.assert_not_called()

    def test_api_tls_failure_does_not_attempt_database(self):
        self.api.side_effect = URLError(ssl.SSLCertVerificationError(1, "private-sentinel"))
        result = diagnose.run()
        self.assertEqual(result["checks"][-1]["phase"], "api_tls_and_credentials")
        self.assertEqual(result["checks"][-1]["category"], "tls")
        self.tcp.assert_not_called()
        self.assertNotIn("private-sentinel", json.dumps(result))

    def test_api_authentication_failure_does_not_attempt_database(self):
        self.api.side_effect = HTTPError("https://example.invalid/private-sentinel", 401, "private-sentinel", {}, None)
        result = diagnose.run()
        self.assertEqual(result["checks"][-1]["category"], "api_authentication")
        self.assertEqual(result["checks"][-1]["http_status"], 401)
        self.tcp.assert_not_called()
        self.assertNotIn("private-sentinel", json.dumps(result))

    def test_tcp_failure_is_network_and_no_password_is_sent(self):
        self.tcp.side_effect = ConnectionRefusedError("private-sentinel")
        result = diagnose.run()
        self.assertEqual(result["checks"][-1]["phase"], "tcp_connect")
        self.assertEqual(result["checks"][-1]["category"], "network")
        self.driver.connect.assert_not_called()

    def test_postgres_tls_rejection_is_not_password_failure(self):
        self.raw.recv.return_value = b"N"
        result = diagnose.run()
        self.assertEqual(result["checks"][-1]["reason"], "postgres_tls_negotiation_rejected")
        self.driver.connect.assert_not_called()

    def test_certificate_failure_closes_socket_without_sending_password(self):
        self.context.wrap_socket.side_effect = ssl.SSLCertVerificationError(1, "private-sentinel")
        result = diagnose.run()
        self.assertEqual(result["checks"][-1]["category"], "tls")
        self.tcp.return_value.__exit__.assert_called_once()
        self.driver.connect.assert_not_called()

    def test_libpq_architecture_failure_is_not_authentication_failure(self):
        def load(name):
            if name == "certifi":
                return SimpleNamespace(where=lambda: "synthetic-ca.pem")
            raise ImportError("incompatible architecture private-sentinel")
        self.load.side_effect = load
        result = diagnose.run()
        self.assertEqual(result["checks"][-1]["reason"], "client_binary_architecture_mismatch")
        self.assertNotIn("private-sentinel", json.dumps(result))

    def test_authentication_error_reports_sqlstate_without_retry(self):
        self.driver.connect.side_effect = UpstreamFailure("private-sentinel", "28P01")
        result = diagnose.run()
        self.assertEqual(result["checks"][-1]["sqlstate"], "28P01")
        self.assertEqual(result["checks"][-1]["category"], "authentication")
        self.assertEqual(self.driver.connect.call_count, 1)

    def test_options_failure_is_distinct_from_successful_minimal_login(self):
        self.driver.connect.side_effect = [self.driver.connect.return_value,
            UpstreamFailure("unsupported startup parameter private-sentinel", "XX000")]
        result = diagnose.run()
        self.assertEqual(result["checks"][-1]["phase"], "libpq_runner_startup_options_connection")
        self.assertEqual(result["checks"][-1]["category"], "startup_configuration")
        self.assertEqual(self.driver.connect.call_count, 2)

    def test_unexpected_session_identity_is_not_acknowledged(self):
        self.conn.execute.return_value.fetchone.return_value = ("private-sentinel", "private-sentinel", "off")
        result = diagnose.run()
        self.assertEqual(result["checks"][-1]["reason"], "unexpected_database_identity_or_readonly_state")
        self.assertNotIn("private-sentinel", json.dumps(result))
        self.assertEqual(self.driver.connect.call_count, 1)
        self.driver.connect.return_value.__exit__.assert_called_once()

    def test_default_ca_failure_is_distinct_from_working_certifi_and_options(self):
        connection = self.driver.connect.return_value
        self.driver.connect.side_effect = [connection, connection,
            UpstreamFailure("root certificate file private-sentinel does not exist")]
        result = diagnose.run()
        self.assertEqual(result["checks"][-1]["phase"], "libpq_existing_runner_default_ca_connection")
        self.assertEqual(result["checks"][-1]["category"], "tls")
        self.assertEqual(self.driver.connect.call_count, 3)
        self.assertNotIn("private-sentinel", json.dumps(result))

    def test_main_emits_only_sanitized_json(self):
        self.driver.connect.side_effect = UpstreamFailure("private-sentinel", "28P01")
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(diagnose.main(), 1)
        result = json.loads(output.getvalue())
        self.assertEqual(result["project"], package.STAGING_REF)
        self.assertNotIn("synthetic-pass", output.getvalue())
        self.assertNotIn("private-sentinel", output.getvalue())
