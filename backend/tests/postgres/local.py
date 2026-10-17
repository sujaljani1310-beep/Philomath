"""Real offline PostgreSQL tests in a bounded, network-disabled container.

Run explicitly: python -m tests.postgres.local. No hosted credentials are read.
"""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
import signal
import subprocess
import time
import unittest
import uuid

from . import package

A = "11111111-1111-4111-8111-111111111111"
B = "22222222-2222-4222-8222-222222222222"


class Database:
    def __init__(self):
        self.name = "philomath-batch4-local-" + uuid.uuid4().hex[:10]
        self.created = False

    def start(self):
        subprocess.run(["docker", "run", "--pull=never", "--detach", "--rm", "--name", self.name,
            "--label", "philomath.batch4=offline-only",
            "--network", "none", "--memory", "512m", "--cpus", "1",
            "--tmpfs", "/var/lib/postgresql/data:rw,size=160m",
            "-e", "POSTGRES_HOST_AUTH_METHOD=trust", "-e", "POSTGRES_DB=philomath_batch4_test",
            package.IMAGE], check=True, stdout=subprocess.DEVNULL, timeout=20)
        self.created = True
        for _ in range(100):
            ready = subprocess.run(["docker", "exec", self.name, "pg_isready", "-U", "postgres",
                "-d", "philomath_batch4_test"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=2)
            if ready.returncode == 0:
                process = subprocess.run(["docker", "exec", self.name, "cat", "/proc/1/comm"],
                    capture_output=True, text=True, timeout=2)
                if process.stdout.strip() == "postgres":
                    return
            time.sleep(0.1)
        raise RuntimeError("Local PostgreSQL did not become ready")

    def stop(self):
        result = subprocess.run(["docker", "rm", "--force", self.name],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15)
        if self.created and result.returncode:
            raise RuntimeError("Verify local fixture container cleanup")

    def execute(self, statement, *, error=None):
        result = subprocess.run(["docker", "exec", "-i", self.name, "psql", "-X", "-qAt",
            "-U", "postgres", "-d", "philomath_batch4_test", "-v", "ON_ERROR_STOP=1",
            "-v", "VERBOSITY=sqlstate"], input="SET statement_timeout='5s'; SET lock_timeout='2s';\n" + statement,
            text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=12)
        if error:
            if result.returncode == 0 or error not in result.stderr:
                raise AssertionError("Expected SQLSTATE " + error + "; got " + result.stderr[:250])
        elif result.returncode:
            raise AssertionError("Local PostgreSQL command failed: " + result.stderr[:400])
        return result.stdout.strip()

    def transaction(self, body, *, error=None):
        return self.execute("BEGIN; SET LOCAL philomath.batch4_target='offline-local';\n" + body + "\nROLLBACK;", error=error)

    def role(self, role, body, user=A, *, error=None):
        assert role in ("authenticated", "anon", "service_role", "synthetic_auth_admin")
        assert user in (A, B)
        return self.transaction("SET LOCAL ROLE " + role + "; SET LOCAL request.jwt.claim.sub='" + user + "'; " + body, error=error)


DB = None


class PostgreSQLIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        DB.execute(package.sql("bootstrap_local.sql"))
        DB.execute("BEGIN; SET LOCAL philomath.batch4_target='offline-local';" + package.baseline() + "COMMIT;")
        snapshot = json.loads(DB.execute(package.SNAPSHOT))
        package.verify_baseline(snapshot)
        cls.baseline = snapshot
        # Known-policy defect reproduced before hardening, without production data.
        DB.execute(f"""INSERT INTO auth.users VALUES ('{A}'),('{B}');
        INSERT INTO public.conversations(id,user_id) VALUES
          ('owned_A','{A}'),('owned_B','{B}'),('legacy_null',NULL),
          ('legacy_null_child','{A}'),('legacy_mismatch','{B}');
        INSERT INTO public.messages(conversation_id,user_id,role,content) VALUES
          ('owned_A','{A}','user','synthetic A'),('owned_B','{B}','user','synthetic B'),
          ('legacy_null',NULL,'user','synthetic legacy'),
          ('legacy_null_child',NULL,'user','synthetic legacy'),
          ('legacy_mismatch','{A}','user','synthetic inconsistent'),
          (NULL,'{A}','user','synthetic unparented');
        GRANT UPDATE(title) ON public.conversations TO authenticated;
        GRANT REFERENCES(conversation_id) ON public.messages TO PUBLIC;
        """)
        cls.before = cls.fingerprint()
        cls.baseline_exposed = DB.role("authenticated", "SELECT count(*) FROM public.messages WHERE conversation_id='legacy_mismatch';")
        cls.gates = {}
        variants = {
            "noinherit": "CREATE ROLE latent NOLOGIN BYPASSRLS; GRANT latent TO authenticated WITH INHERIT FALSE;",
            "privileged_rpc": "CREATE FUNCTION public.unsafe_fixture() RETURNS int LANGUAGE sql SECURITY DEFINER AS 'SELECT 1';",
            "exposed_view": "CREATE VIEW public.unsafe_view AS SELECT * FROM public.conversations; GRANT SELECT ON public.unsafe_view TO authenticated;",
            "backend_privilege": "REVOKE INSERT ON public.conversations FROM service_role;",
            "disabled_fk": "ALTER TABLE public.messages DISABLE TRIGGER ALL;",
        }
        for key, setup in variants.items():
            DB.transaction(setup + package.hardening(), error="P0001")
            cls.gates[key] = True
        DB.execute("BEGIN; SET LOCAL philomath.batch4_target='offline-local';" + package.hardening() + "COMMIT;")

    @staticmethod
    def fingerprint():
        return DB.execute("SELECT md5(jsonb_build_array((SELECT jsonb_agg(x ORDER BY id) FROM public.conversations x),(SELECT jsonb_agg(x ORDER BY id) FROM public.messages x))::text);")

    def test_01_complete_exported_baseline_matches(self):
        package.verify_baseline(self.baseline)
        self.assertEqual(len(self.baseline["columns"]), 22)
        self.assertEqual(len(self.baseline["constraints"]), 9)

    def test_02_existing_rows_unchanged_by_migrations(self):
        self.assertEqual(self.fingerprint(), self.before)

    def test_03_latent_noinherit_membership_rejected(self):
        self.assertTrue(self.gates["noinherit"])

    def test_04_privileged_rpc_requires_review(self):
        self.assertTrue(self.gates["privileged_rpc"])

    def test_05_exposed_view_requires_review(self):
        self.assertTrue(self.gates["exposed_view"])

    def test_06_effective_table_and_column_privileges(self):
        DB.transaction(package.sql("target_guard.sql") + package.sql("catalog_contract.sql"))

    def test_07_parent_policy_hides_inconsistent_legacy_child(self):
        self.assertEqual(self.baseline_exposed, "1")
        self.assertEqual(DB.role("authenticated", "SELECT count(*) FROM public.messages WHERE conversation_id='legacy_mismatch';"), "0")

    def test_08_symmetric_own_row_reads_and_foreign_isolation(self):
        for user, own, other in ((A, "owned_A", "owned_B"), (B, "owned_B", "owned_A")):
            for table, field in (("conversations", "id"), ("messages", "conversation_id")):
                self.assertEqual(DB.role("authenticated", f"SELECT count(*) FROM public.{table} WHERE {field}='{own}';", user), "1")
                self.assertEqual(DB.role("authenticated", f"SELECT count(*) FROM public.{table} WHERE {field}='{other}';", user), "0")

    def test_09_browser_dml_and_truncate_denied(self):
        for role in ("anon", "authenticated"):
            for table in package.TABLES:
                statements = [f"DELETE FROM public.{table};", f"TRUNCATE public.{table};"]
                statements += [f"INSERT INTO public.{table} DEFAULT VALUES;", f"UPDATE public.{table} SET user_id='{A}';"]
                for statement in statements:
                    with self.subTest(role=role, table=table, statement=statement.split()[0]):
                        DB.role(role, statement, error="42501")

    def test_10_anon_reads_and_integration_reads_denied(self):
        for table in package.TABLES:
            DB.role("anon", f"SELECT * FROM public.{table};", error="42501")
        DB.role("authenticated", "SELECT encrypted_api_key FROM public.user_ai_integrations;", error="42501")

    def test_11_new_ownerless_conversation_rejected(self):
        DB.role("service_role", "INSERT INTO public.conversations(id) VALUES ('new_null');", error="23514")

    def test_12_new_ownerless_unparented_foreign_messages_rejected(self):
        for parent, user, code in (("'owned_A'", "NULL", "23514"), ("NULL", f"'{A}'", "23514"),
                                   ("'owned_B'", f"'{A}'", "23503"), ("'missing'", f"'{A}'", "23503")):
            DB.role("service_role", f"INSERT INTO public.messages(conversation_id,user_id,role,content) VALUES ({parent},{user},'user','synthetic');", error=code)

    def test_13_owner_changes_rejected(self):
        DB.role("service_role", f"UPDATE public.conversations SET user_id='{B}' WHERE id='owned_A';", error="23514")
        DB.role("service_role", f"UPDATE public.messages SET user_id='{B}' WHERE conversation_id='owned_A';", error="23514")

    def test_14_reparenting_to_foreign_parent_rejected(self):
        DB.role("service_role", "UPDATE public.messages SET conversation_id='owned_B' WHERE conversation_id='owned_A';", error="23503")

    def test_15_legacy_updates_and_deletes_rejected_without_changes(self):
        for statement in ("UPDATE public.conversations SET title='synthetic' WHERE id='legacy_null';",
                          "DELETE FROM public.conversations WHERE id='legacy_null';",
                          "UPDATE public.messages SET content='synthetic' WHERE user_id IS NULL;",
                          "DELETE FROM public.messages WHERE user_id IS NULL;"):
            DB.role("service_role", statement, error="23514")
        self.assertEqual(self.fingerprint(), self.before)

    def test_16_parent_delete_preserves_null_and_foreign_children(self):
        for parent in ("legacy_null_child", "legacy_mismatch"):
            DB.role("service_role", f"DELETE FROM public.conversations WHERE id='{parent}';", error="23514")
        self.assertEqual(self.fingerprint(), self.before)

    def test_17_valid_cascade_and_transaction_rollback(self):
        self.assertEqual(DB.role("service_role", "DELETE FROM public.conversations WHERE id='owned_B'; SELECT count(*) FROM public.messages WHERE conversation_id='owned_B';"), "0")
        self.assertEqual(self.fingerprint(), self.before)

    def test_18_auth_user_delete_with_legacy_conflict_rolls_back(self):
        DB.role("synthetic_auth_admin", f"DELETE FROM auth.users WHERE id='{A}';", error="23514")
        self.assertEqual(self.fingerprint(), self.before)

    def test_19_original_fk_actions_and_not_valid_state_preserved(self):
        self.assertEqual(DB.execute("SELECT count(*) FROM pg_constraint WHERE contype='f' AND connamespace='public'::regnamespace AND confdeltype='c' AND confupdtype='a' AND NOT condeferrable;"), "5")
        self.assertEqual(DB.execute("SELECT convalidated FROM pg_constraint WHERE conname='messages_parent_owner_fkey';"), "f")

    def test_20_trigger_search_path_invoker_and_execute_privileges(self):
        self.assertEqual(DB.execute("SELECT count(*) FROM pg_proc WHERE proname IN ('philomath_guard_message','philomath_guard_conversation') AND NOT prosecdef AND proconfig=ARRAY['search_path=\"\"'];"), "2")
        for role in ("anon", "authenticated"):
            self.assertEqual(DB.execute(f"SELECT has_function_privilege('{role}','public.philomath_guard_message()','EXECUTE');"), "f")

    def test_21_integration_upsert_uniqueness_and_stable_id(self):
        statement = f"""INSERT INTO public.user_ai_integrations(user_id,provider,display_name,encrypted_api_key,api_key_last4)
            VALUES ('{A}','gemini','Gemini','synthetic-ciphertext','1234')
            ON CONFLICT(user_id,provider) DO UPDATE SET encrypted_api_key=excluded.encrypted_api_key RETURNING id;"""
        result = DB.role("service_role", statement + statement).splitlines()
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0], result[1])

    def test_22_partial_write_failure_rolls_back_only_its_transaction(self):
        DB.role("service_role", f"INSERT INTO public.conversations(id,user_id) VALUES ('partial','{A}'); INSERT INTO public.messages(conversation_id,user_id,role,content) VALUES ('partial','{B}','user','synthetic');", error="23503")
        self.assertEqual(DB.execute("SELECT count(*) FROM public.conversations WHERE id='partial';"), "0")

    def test_23_commit_then_lost_ack_is_not_exactly_once(self):
        name = "ambiguous_" + uuid.uuid4().hex
        statement = f"INSERT INTO public.conversations(id,user_id) VALUES ('{name}','{A}');"
        DB.execute(statement)  # Deliberately discard successful command output.
        DB.execute(statement, error="23505")  # Probe independent resubmission, not a runner retry.
        self.assertEqual(DB.execute(f"SELECT count(*) FROM public.conversations WHERE id='{name}';"), "1")
        DB.execute(f"DELETE FROM public.conversations WHERE id='{name}';")

    def race(self, insert_first):
        name = "race_" + uuid.uuid4().hex
        DB.execute(f"INSERT INTO public.conversations(id,user_id) VALUES ('{name}','{A}');")
        insert = f"INSERT INTO public.messages(conversation_id,user_id,role,content) VALUES ('{name}','{A}','user','synthetic');"
        delete = f"DELETE FROM public.conversations WHERE id='{name}';"
        lock = 40000 + int(uuid.uuid4().hex[:4], 16)
        first = insert if insert_first else delete
        second = delete if insert_first else insert
        with ThreadPoolExecutor(max_workers=2) as pool:
            held = pool.submit(DB.execute, f"BEGIN; {first} SELECT pg_advisory_xact_lock({lock}); SELECT pg_sleep(1); COMMIT;")
            for _ in range(20):
                if DB.execute(f"SELECT count(*) FROM pg_locks WHERE locktype='advisory' AND objid={lock} AND granted;") == "1":
                    break
                time.sleep(0.02)
            else:
                raise AssertionError("Race barrier was not reached")
            follower = pool.submit(DB.execute, second, error=None if insert_first else "23503")
            held.result(timeout=10)
            follower.result(timeout=10)
        self.assertEqual(DB.execute(f"SELECT count(*) FROM public.messages WHERE conversation_id='{name}';"), "0")

    def test_24_insert_first_blocks_delete_then_cascades(self):
        self.race(True)

    def test_25_delete_first_blocks_insert_then_rejects_orphan(self):
        self.race(False)

    def test_26_concurrent_upserts_keep_one_identity(self):
        statement = f"INSERT INTO public.user_ai_integrations(user_id,provider,display_name,encrypted_api_key,api_key_last4) VALUES ('{B}','grok','Grok','synthetic-ciphertext','1234') ON CONFLICT(user_id,provider) DO UPDATE SET api_key_last4=excluded.api_key_last4 RETURNING id;"
        with ThreadPoolExecutor(max_workers=2) as pool:
            values = list(pool.map(DB.execute, (statement, statement)))
        self.assertEqual(values[0], values[1])
        self.assertEqual(DB.execute(f"SELECT count(*) FROM public.user_ai_integrations WHERE user_id='{B}' AND provider='grok';"), "1")
        DB.execute(f"DELETE FROM public.user_ai_integrations WHERE user_id='{B}' AND provider='grok';")

    def test_27_missing_execution_acknowledgment_aborts(self):
        DB.execute(package.sql("target_guard.sql"), error="P0001")

    def test_28_independent_message_resubmission_is_not_deduplicated(self):
        result = DB.role("service_role", f"""INSERT INTO public.messages(conversation_id,user_id,role,content)
            VALUES ('owned_A','{A}','user','repeated synthetic'),('owned_A','{A}','user','repeated synthetic');
            SELECT count(DISTINCT id) FROM public.messages WHERE content='repeated synthetic';""")
        self.assertEqual(result, "2")  # Explicit limitation; no exactly-once promise.

    def test_29_statement_timeout_rolls_back_and_connection_recovers(self):
        DB.execute(f"BEGIN; SET LOCAL statement_timeout='30ms'; INSERT INTO public.conversations(id,user_id) VALUES ('timeout_fixture','{A}'); SELECT pg_sleep(1); COMMIT;", error="57014")
        self.assertEqual(DB.execute("SELECT count(*) FROM public.conversations WHERE id='timeout_fixture';"), "0")
        self.assertEqual(DB.execute("SELECT 1;"), "1")

    def test_30_valid_auth_user_cascade(self):
        user = "33333333-3333-4333-8333-333333333333"
        self.assertEqual(DB.transaction(f"""INSERT INTO auth.users VALUES ('{user}');
            INSERT INTO public.conversations(id,user_id) VALUES ('auth_cascade','{user}');
            INSERT INTO public.messages(conversation_id,user_id,role,content) VALUES ('auth_cascade','{user}','user','synthetic');
            SET LOCAL ROLE synthetic_auth_admin; DELETE FROM auth.users WHERE id='{user}';
            RESET ROLE; SELECT count(*) FROM public.messages WHERE conversation_id='auth_cascade';"""), "0")

    def test_31_child_index_supports_parent_owner_lookup(self):
        plan = DB.transaction(f"SET LOCAL enable_seqscan=off; EXPLAIN SELECT id FROM public.messages WHERE conversation_id='owned_A' AND user_id='{A}';")
        self.assertIn("messages_conversation_user_idx", plan)

    def test_32_each_exported_catalog_section_detects_drift(self):
        for section in ("columns", "constraints", "indexes", "policies", "triggers", "functions"):
            snapshot = deepcopy(self.baseline)
            snapshot[section] = []
            with self.subTest(section=section), self.assertRaises(ValueError):
                package.verify_baseline(snapshot)

    def test_33_missing_backend_privilege_rolls_back_migration(self):
        self.assertTrue(self.gates["backend_privilege"])

    def test_34_disabled_fk_triggers_block_hardening(self):
        self.assertTrue(self.gates["disabled_fk"])

    def test_35_hosted_sql_recipe_on_local_auth_model(self):
        settings = f"SET LOCAL philomath.batch4_user_a='{A}'; SET LOCAL philomath.batch4_user_b='{B}'; SET LOCAL philomath.batch4_parent_a='owned_A'; SET LOCAL philomath.batch4_parent_b='owned_B';"
        DB.transaction(settings + package.sql("target_guard.sql") + package.sql("two_user_permissions.sql"))


def main():
    global DB
    DB = Database()
    def terminate(*_):
        raise SystemExit(143)
    old_handler = signal.signal(signal.SIGTERM, terminate)
    try:
        DB.start()
        result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(PostgreSQLIntegrationTests))
        print("LOCAL POSTGRESQL ONLY: hosted Supabase Auth/PostgREST tests NOT RUN")
        return 0 if result.wasSuccessful() else 1
    finally:
        try:
            DB.stop()
        finally:
            signal.signal(signal.SIGTERM, old_handler)


if __name__ == "__main__":
    raise SystemExit(main())
