"""Credential-free regressions for the explicit database verification package."""
from contextlib import redirect_stdout
import io
import json
import os
from unittest.mock import MagicMock, patch
import unittest

from tests.postgres import package, staging


URI = "postgresql://postgres.tdiqzmaqvoynspmilwra:synthetic-pass@aws-0-test.pooler.supabase.com:5432/postgres"


class StagingTargetSafetyTests(unittest.TestCase):
    def test_staging_pooler_and_password_decoding(self):
        target = staging.target_from_uri(URI.replace("synthetic-pass", "synthetic%40pass"))
        self.assertEqual(target.user, "postgres." + package.STAGING_REF)
        self.assertEqual(target.password, "synthetic@pass")
        self.assertNotIn("synthetic@pass", repr(target))

    def test_exact_staging_direct_endpoint(self):
        target = staging.target_from_uri("postgresql://postgres:synthetic-pass@db.tdiqzmaqvoynspmilwra.supabase.co:5432/postgres")
        self.assertEqual(target.host, "db." + package.STAGING_REF + ".supabase.co")

    def test_ambient_host_service_and_option_overrides_rejected(self):
        for name in ("PGHOSTADDR", "PGSERVICE", "PGOPTIONS"):
            with patch.dict(os.environ, {name: "synthetic-override"}), self.assertRaises(ValueError):
                staging.reject_ambient_libpq_settings()

    def test_production_never_becomes_connection_target(self):
        with self.assertRaises(ValueError):
            staging.target_from_uri(URI.replace(package.STAGING_REF, package.PRODUCTION_REF))

    def test_wrong_username_host_scheme_database_and_port_rejected(self):
        variants = (URI.replace("postgres.tdiqzmaqvoynspmilwra", "postgres.other"),
            URI.replace("pooler.supabase.com", "pooler.supabase.com.evil.invalid"),
            URI.replace("postgresql://", "https://"), URI.replace(":5432/", ":6543/"),
            URI.rsplit("/", 1)[0] + "/another")
        for value in variants:
            with self.subTest(value=value), self.assertRaises(ValueError):
                staging.target_from_uri(value)

    def test_query_overrides_and_tls_downgrade_rejected(self):
        for query in ("host=evil.invalid", "service=production", "sslmode=disable", "sslmode=require&sslmode=disable", "options=-c%20role=other"):
            with self.subTest(query=query), self.assertRaises(ValueError):
                staging.target_from_uri(URI + "?" + query)

    def test_malformed_and_control_characters_do_not_leak_in_error(self):
        for value in ("private-sentinel", URI.replace("synthetic-pass", "[private-sentinel]"), URI + "\n", URI.replace("synthetic-pass", "%QZ")):
            with self.assertRaises(ValueError) as caught:
                staging.target_from_uri(value)
            self.assertNotIn("private-sentinel", str(caught.exception))

    def test_redirects_never_forward_staging_keys(self):
        for status in (301, 302, 303, 307, 308):
            with self.subTest(status=status), self.assertRaises(ValueError):
                staging.NoRedirect().redirect_request(None, None, status, "", {}, "https://evil.invalid")

    def test_missing_bindings_fail_before_api_and_no_values_logged(self):
        output = io.StringIO()
        with patch.dict(os.environ, {}, clear=True), patch.object(staging, "verify_api") as api, redirect_stdout(output):
            self.assertEqual(staging.main([]), 1)
        api.assert_not_called()
        self.assertEqual(json.loads(output.getvalue())["write_outcome"], "no write attempted")

    def test_inspect_uses_readonly_and_never_executes_write_candidates(self):
        connection = MagicMock()
        connection.execute.side_effect = lambda sql, *args: MagicMock(fetchone=lambda: ("postgres", "postgres") if "current_database" in sql else ({},))
        driver = MagicMock()
        driver.connect.return_value.__enter__.return_value = connection
        with patch.dict(os.environ, {"PHILOMATH_STAGING_DATABASE_URL": URI}), patch.dict("sys.modules", {"psycopg": driver}), patch.object(staging, "verify_api"), patch.object(package, "hardening") as ddl, redirect_stdout(io.StringIO()):
            self.assertEqual(staging.main([]), 0)
        ddl.assert_not_called()
        self.assertTrue(any("SET TRANSACTION READ ONLY" in c.args[0] for c in connection.execute.call_args_list))
        self.assertEqual(driver.connect.call_args.kwargs["sslmode"], "verify-full")

    def test_ambiguous_ddl_is_never_retried_or_logged(self):
        connection = MagicMock()
        def execute(sql, *args):
            if sql == "synthetic DDL":
                raise TimeoutError("private-sentinel")
            return MagicMock(fetchone=lambda: ("postgres", "postgres") if "current_database" in sql else ({},))
        connection.execute.side_effect = execute
        driver = MagicMock()
        driver.connect.return_value.__enter__.return_value = connection
        output = io.StringIO()
        with patch.dict(os.environ, {"PHILOMATH_STAGING_DATABASE_URL": URI}), patch.dict("sys.modules", {"psycopg": driver}), patch.object(staging, "verify_api"), patch.object(package, "verify_baseline"), patch.object(package, "hardening", return_value="synthetic DDL") as ddl, redirect_stdout(output):
            self.assertEqual(staging.main(["--action", "harden"]), 1)
        ddl.assert_called_once()
        self.assertEqual(sum(c.args[0] == "synthetic DDL" for c in connection.execute.call_args_list), 1)
        self.assertNotIn("private-sentinel", output.getvalue())
        self.assertIn("unconfirmed", output.getvalue())
        calls = [c.args[0] for c in connection.execute.call_args_list]
        self.assertLess(next(i for i, s in enumerate(calls) if "ACCESS EXCLUSIVE" in s), calls.index("synthetic DDL"))


class SQLPackageSafetyTests(unittest.TestCase):
    def test_no_sql_is_installed_as_browser_execution_endpoint(self):
        script = package.hardening().lower()
        self.assertNotRegex(script, r"language\s+plpgsql\s+security\s+definer")
        self.assertNotIn("execute sql", script)
        self.assertIn("security invoker set search_path = ''", script)

    def test_both_steps_are_composed_under_the_same_guard(self):
        text = package.hardening()
        self.assertLess(text.index("DO $target$"), text.index("REVOKE ALL PRIVILEGES"))
        self.assertIn("FOREIGN KEY (conversation_id,user_id)", text)
        self.assertNotIn("COMMIT;", text)

    def test_missing_catalog_details_are_not_certified(self):
        with self.assertRaises(ValueError):
            package.verify_baseline({})

    def test_schema_metadata_contains_only_original_export_rows(self):
        metadata = json.loads(package.sql("schema_metadata.json"))
        self.assertEqual(sorted(len(x["rows"]) for x in metadata["exports"].values()), [1, 20, 30, 70])
        self.assertTrue(metadata["unknown"])

    def test_container_is_pinned_network_disabled_and_memory_bounded(self):
        from tests.postgres.local import Database
        db = Database()
        with patch("subprocess.run") as run, patch("time.sleep"):
            run.return_value.returncode = 0
            run.return_value.stdout = "postgres\n"
            db.start()
            args = run.call_args_list[0].args[0]
        self.assertEqual(args[args.index("--network") + 1], "none")
        self.assertIn("--memory", args)
        self.assertIn("--cpus", args)
        self.assertIn("--pull=never", args)
        self.assertIn("@sha256:", args[-1])
