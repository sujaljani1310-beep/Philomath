"""Offline safety tests for plans/drafts; these never execute PostgreSQL SQL."""

from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import patch

from tests import postgres_plan


ROOT = Path(__file__).resolve().parents[2]


class PostgreSQLScaffoldSafetyTests(unittest.TestCase):
    def test_default_plan_blocks_on_missing_canonical_metadata(self):
        result = postgres_plan.build_plan()
        self.assertFalse(result["execution_enabled"])
        self.assertFalse(result["schema_verified"])
        self.assertEqual(result["status"], "BLOCKED")
        self.assertEqual(set(result["missing_evidence"]), set(postgres_plan.REQUIRED_EVIDENCE))

    def test_complete_keys_do_not_falsely_certify_schema_or_enable_execution(self):
        result = postgres_plan.build_plan({key: [] for key in postgres_plan.REQUIRED_EVIDENCE})
        self.assertEqual(result["status"], "REVIEW_REQUIRED")
        self.assertFalse(result["schema_verified"])
        self.assertFalse(result["execution_enabled"])

    def test_partial_or_null_evidence_remains_blocked(self):
        result = postgres_plan.build_plan({"columns": [], "constraints": None})
        self.assertNotIn("columns", result["missing_evidence"])
        self.assertIn("constraints", result["missing_evidence"])
        self.assertIn("policies", result["missing_evidence"])

    def test_metadata_must_be_an_object(self):
        for value in ([], "synthetic", 42):
            with self.subTest(value=value), self.assertRaises(ValueError):
                postgres_plan.build_plan(value)

    def test_environment_credentials_never_choose_target_or_trigger_connection(self):
        output = io.StringIO()
        with patch.dict(os.environ, {"DATABASE_URL": "postgres://synthetic-private.invalid/prod", "PGHOST": "synthetic-private.invalid", "SUPABASE_URL": "https://synthetic-private.invalid"}), patch("socket.socket.connect", side_effect=AssertionError("no connections")), patch("subprocess.run", side_effect=AssertionError("no database tools")), redirect_stdout(output):
            self.assertEqual(postgres_plan.main([]), 0)
        self.assertFalse(json.loads(output.getvalue())["execution_enabled"])
        self.assertNotIn("synthetic-private", output.getvalue())

    def test_no_execute_or_connection_option_is_accepted(self):
        for argument in ("--execute", "--db-url", "--host"):
            with self.subTest(argument=argument), redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as caught:
                postgres_plan.main([argument])
            self.assertEqual(caught.exception.code, 2)

    def test_supplied_metadata_is_hashed_without_echoing_sensitive_bodies(self):
        with tempfile.TemporaryDirectory() as folder:
            artifact = Path(folder) / "schema.json"
            artifact.write_text(json.dumps({"functions": "synthetic-private-function-body"}))
            output = io.StringIO()
            with redirect_stdout(output):
                postgres_plan.main(["--metadata", str(artifact)])
        self.assertEqual(len(json.loads(output.getvalue())["artifact_sha256"]), 64)
        self.assertNotIn("synthetic-private", output.getvalue())

    def test_nonregular_symlink_and_oversized_artifacts_are_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "schema.json"
            target.write_text("{}")
            link = Path(folder) / "link"
            link.symlink_to(target)
            large = Path(folder) / "large"
            large.write_bytes(b"x" * (2 * 1024 * 1024 + 1))
            fifo = Path(folder) / "pipe"
            os.mkfifo(fifo)
            for path in (Path(folder), link, large, fifo):
                with self.subTest(path=path.name), redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as caught:
                    postgres_plan.main(["--metadata", str(path)])
                self.assertEqual(caught.exception.code, 2)

    def test_candidates_require_explicit_verified_target_before_operations(self):
        artifacts = list((ROOT / "supabase/drafts").glob("*.sql.draft"))
        self.assertEqual(len(artifacts), 2)
        for path in artifacts:
            with self.subTest(path=path.name):
                sql = re.sub(r"--[^\n]*", "", path.read_text()).strip()
                self.assertTrue(sql.startswith("DO $target$"))
                guard_end = sql.index("$target$;", sql.index("DO $target$") + 1)
                self.assertIn("RAISE EXCEPTION", sql[:guard_end])
                self.assertIn("tdiqzmaqvoynspmilwra", sql[:guard_end])
                self.assertIn("current_database() = 'philomath_batch4_test'", sql[:guard_end])
                self.assertNotRegex(sql, r"\bCOMMIT\b")

    def test_candidates_preserve_original_fks_and_legacy_records(self):
        self.assertEqual([p.name for p in (ROOT / "supabase/migrations").glob("*.sql")], ["001_google_auth_byok.sql"])
        draft = ROOT / "supabase/drafts/003_parent_owner_integrity.sql.draft"
        sql = re.sub(r"--[^\n]*", "", draft.read_text())
        self.assertNotRegex(sql, r"\bDROP\b|\b(?:UPDATE|DELETE FROM|INSERT INTO)\s+public\.")
        self.assertIn("ON DELETE CASCADE ON UPDATE NO ACTION NOT DEFERRABLE NOT VALID", sql)
        self.assertNotIn("SET NOT NULL", sql)
