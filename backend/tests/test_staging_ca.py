"""Offline CA configuration regressions; only synthetic certificates and mocks."""
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
import io
import json
import os
from pathlib import Path
import ssl
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import MagicMock, patch

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from tests.postgres import hosted, package, staging
from tests.test_postgres_package import URI


def write_synthetic_ca(path):
    """Write a public test certificate; the synthetic private key stays in memory."""
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Synthetic Batch 4 CA")])
    now = datetime.now(timezone.utc)
    certificate = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
        .public_key(key.public_key()).serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1)).not_valid_after(now + timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256()))
    path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    return certificate


class StagingCATests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.dict(os.environ, {}, clear=True))
        self.directory = Path(self.enterContext(TemporaryDirectory()))
        self.ca = self.directory / "synthetic-ca.pem"
        self.certificate = write_synthetic_ca(self.ca)

    def configure(self, value=None):
        os.environ[staging.POSTGRES_CA_ENV] = str(self.ca) if value is None else value

    def test_absent_configuration_preserves_each_callers_default(self):
        self.assertEqual(staging.postgres_ca_file("existing-default.pem"), "existing-default.pem")
        self.assertIsNone(staging.postgres_ca_file(None))

    def test_explicit_pem_is_parsed_and_selected_over_default(self):
        self.configure()
        self.assertEqual(staging.postgres_ca_file("unused-default.pem"), str(self.ca.resolve()))

    def test_literal_tilde_path_is_expanded(self):
        self.configure("~/synthetic-ca.pem")
        with patch("os.path.expanduser", side_effect=lambda value: value.replace("~", str(self.directory), 1)) as expand:
            self.assertEqual(staging.postgres_ca_file(None), str(self.ca.resolve()))
        expand.assert_called_once()

    def test_empty_missing_directory_and_control_characters_fail_closed(self):
        for value in ("", " ", str(self.directory), str(self.directory / "private-sentinel-missing.pem"),
                      str(self.ca) + "\n"):
            with self.subTest(case=repr(value)):
                self.configure(value)
                with self.assertRaises(staging.PostgresCAError) as caught:
                    staging.postgres_ca_file("unused-default.pem")
                self.assertNotIn("private-sentinel", str(caught.exception))
                self.assertNotIn(str(self.directory), str(caught.exception))

    def test_malformed_or_empty_pem_never_uses_default(self):
        for contents in (b"", b"private-sentinel", b"-----BEGIN CERTIFICATE-----\nprivate-sentinel"):
            self.ca.write_bytes(contents)
            self.configure()
            with self.assertRaises(staging.PostgresCAError) as caught:
                staging.postgres_ca_file("unused-default.pem")
            self.assertNotIn("private-sentinel", str(caught.exception))

    def test_unreadable_file_error_is_sanitized(self):
        self.configure()
        with patch.object(ssl.SSLContext, "load_verify_locations", side_effect=PermissionError("private-sentinel")):
            with self.assertRaises(staging.PostgresCAError) as caught:
                staging.postgres_ca_file(None)
        self.assertNotIn("private-sentinel", str(caught.exception))
        self.assertIsNone(caught.exception.__cause__)

    def test_database_ca_does_not_enter_https_trust_store(self):
        self.configure()
        context = staging.api_tls_context()
        self.assertTrue(context.check_hostname)
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertGreaterEqual(context.minimum_version, ssl.TLSVersion.TLSv1_2)
        self.assertNotIn(self.certificate.public_bytes(serialization.Encoding.DER),
                         context.get_ca_certs(binary_form=True))

    def test_inspect_passes_explicit_ca_and_executes_only_readonly_queries(self):
        self.configure()
        os.environ["PHILOMATH_STAGING_DATABASE_URL"] = URI
        conn = MagicMock()
        conn.pgconn.ssl_in_use = True
        conn.execute.side_effect = lambda sql: MagicMock(fetchone=lambda:
            ("postgres", "postgres") if "current_database" in sql else ({},))
        driver = MagicMock()
        driver.connect.return_value.__enter__.return_value = conn
        output = io.StringIO()
        with patch.dict("sys.modules", {"psycopg": driver}), patch.object(staging, "verify_api"), \
             patch.object(package, "baseline") as baseline, patch.object(package, "hardening") as harden, \
             redirect_stdout(output):
            self.assertEqual(staging.main(["--action", "inspect"]), 0)
        driver.connect.assert_called_once()
        parameters = driver.connect.call_args.kwargs
        self.assertEqual(parameters["sslmode"], "verify-full")
        self.assertEqual(parameters["sslrootcert"], str(self.ca.resolve()))
        self.assertEqual(parameters["host"], "aws-0-test.pooler.supabase.com")
        self.assertEqual(parameters["user"], "postgres." + package.STAGING_REF)
        self.assertEqual([c.args[0] for c in conn.execute.call_args_list], [
            "SET TRANSACTION READ ONLY", "SELECT current_database(),current_user", package.SNAPSHOT])
        self.assertEqual(json.loads(output.getvalue())["project"], package.STAGING_REF)
        self.assertNotIn(str(self.ca), output.getvalue())
        baseline.assert_not_called()
        harden.assert_not_called()
        driver.connect.return_value.__exit__.assert_called_once()

    def test_invalid_ca_blocks_inspection_and_hosted_runner_before_network(self):
        self.configure(str(self.directory / "private-sentinel.pem"))
        os.environ["PHILOMATH_STAGING_DATABASE_URL"] = URI
        driver = MagicMock()
        output = io.StringIO()
        with patch.dict("sys.modules", {"psycopg": driver}), patch.object(staging, "verify_api") as api, \
             patch.object(hosted.API, "request") as request, redirect_stdout(output):
            self.assertEqual(staging.main(["--action", "inspect"]), 1)
            self.assertEqual(hosted.main(), 1)
        api.assert_not_called()
        request.assert_not_called()
        driver.connect.assert_not_called()
        self.assertNotIn("private-sentinel", output.getvalue())
        self.assertNotIn("synthetic-pass", output.getvalue())

    def test_production_target_rejected_even_with_valid_ca(self):
        self.configure()
        os.environ["PHILOMATH_STAGING_DATABASE_URL"] = URI.replace(package.STAGING_REF, package.PRODUCTION_REF)
        with patch.object(staging, "postgres_ca_file") as ca, patch.object(staging, "verify_api") as api, \
             patch.object(hosted.API, "request") as request, redirect_stdout(io.StringIO()):
            self.assertEqual(staging.main(["--action", "inspect"]), 1)
            self.assertEqual(hosted.main(), 1)
        ca.assert_not_called()
        api.assert_not_called()
        request.assert_not_called()

    def test_tls_failure_never_retries_with_another_ca_or_weaker_mode(self):
        self.configure()
        os.environ["PHILOMATH_STAGING_DATABASE_URL"] = URI
        driver = MagicMock()
        driver.connect.side_effect = ssl.SSLCertVerificationError(1, "private-sentinel")
        with patch.dict("sys.modules", {"psycopg": driver}), patch.object(staging, "verify_api"), \
             patch.object(hosted.API, "request") as request, redirect_stdout(io.StringIO()) as output:
            self.assertEqual(staging.main(["--action", "inspect"]), 1)
            self.assertEqual(hosted.main(), 1)
        self.assertEqual(driver.connect.call_count, 2)  # One attempt per tool.
        for call in driver.connect.call_args_list:
            self.assertEqual(call.kwargs["sslmode"], "verify-full")
            self.assertEqual(call.kwargs["sslrootcert"], str(self.ca.resolve()))
        request.assert_not_called()
        self.assertNotIn("private-sentinel", output.getvalue())

    def test_hosted_runner_passes_explicit_ca_before_any_synthetic_writes(self):
        self.configure()
        os.environ["PHILOMATH_STAGING_DATABASE_URL"] = URI
        driver = MagicMock()
        driver.connect.side_effect = TimeoutError("private-sentinel")
        with patch.dict("sys.modules", {"psycopg": driver}), patch.object(staging, "verify_api"), \
             patch.object(hosted.API, "request") as request, redirect_stdout(io.StringIO()):
            self.assertEqual(hosted.main(), 1)
        driver.connect.assert_called_once()
        self.assertEqual(driver.connect.call_args.kwargs["sslrootcert"], str(self.ca.resolve()))
        self.assertEqual(driver.connect.call_args.kwargs["sslmode"], "verify-full")
        request.assert_not_called()

    def test_api_credentials_use_public_ca_context_and_keep_redirect_guard(self):
        self.configure()
        os.environ["PHILOMATH_STAGING_PUBLIC_KEY"] = "synthetic-public"
        os.environ["PHILOMATH_STAGING_SERVICE_ROLE_KEY"] = "synthetic-service"
        response = MagicMock()
        response.__enter__.return_value = response
        response.status = 200
        response.headers = {}
        context = staging.api_tls_context()
        with patch.object(staging, "api_tls_context", return_value=context) as api_context, \
             patch.object(staging, "build_opener") as opener, \
             patch.object(staging, "postgres_ca_file", side_effect=AssertionError("Database CA used for HTTPS")):
            opener.return_value.open.return_value = response
            staging.verify_api()
        api_context.assert_called_once()
        self.assertIsInstance(opener.call_args.args[0], staging.NoRedirect)
        self.assertIs(opener.call_args.args[1]._context, context)
        for call in opener.return_value.open.call_args_list:
            self.assertTrue(call.args[0].full_url.startswith("https://" + package.STAGING_REF + ".supabase.co/"))

    def test_hosted_api_uses_same_separate_public_ca_context(self):
        self.configure()
        response = MagicMock()
        response.__enter__.return_value = response
        response.code = 200
        response.read.return_value = b"[]"
        context = staging.api_tls_context()
        with patch.object(staging, "api_tls_context", return_value=context) as api_context, \
             patch.object(hosted, "build_opener") as opener:
            opener.return_value.open.return_value = response
            self.assertEqual(hosted.API().request("GET", "/rest/v1/messages", "synthetic-key"), (200, []))
        api_context.assert_called_once()
        self.assertIsInstance(opener.call_args.args[0], staging.NoRedirect)
        self.assertIs(opener.call_args.args[1]._context, context)
