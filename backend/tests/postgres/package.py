"""Fixed SQL artifacts and catalog contract shared by local/staging runners."""
import json
from pathlib import Path
import re

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
STAGING_REF = "tdiqzmaqvoynspmilwra"
PRODUCTION_REF = "inlzipnacliyhdahwcak"
TABLES = ("conversations", "messages", "user_ai_integrations")
IMAGE = "postgres:17.6-bookworm@sha256:f3bd19c606e442c3d7bdfa8002e03fe260a1023351e0ea4598032022b68dd6e3"


def sql(name):
    return (HERE / name).read_text()


def baseline():
    return sql("target_guard.sql") + sql("baseline.sql") + (
        ROOT / "supabase/migrations/001_google_auth_byok.sql"
    ).read_text() + sql("baseline_grants.sql")


def hardening():
    return sql("target_guard.sql") + sql("preflight.sql") + "\n".join(
        (ROOT / "supabase/drafts" / name).read_text() for name in (
            "002_backend_only_writes.sql.draft", "003_parent_owner_integrity.sql.draft",
        )
    ) + sql("catalog_contract.sql")


SNAPSHOT = """
SELECT json_build_object(
 'columns', (SELECT json_agg(json_build_array(c.relname,a.attname,
    format_type(a.atttypid,a.atttypmod),a.attnotnull,pg_get_expr(d.adbin,d.adrelid)))
    FROM pg_attribute a JOIN pg_class c ON c.oid=a.attrelid
    JOIN pg_namespace n ON n.oid=c.relnamespace
    LEFT JOIN pg_attrdef d ON d.adrelid=c.oid AND d.adnum=a.attnum
    WHERE n.nspname='public' AND c.relname IN ('conversations','messages','user_ai_integrations')
    AND a.attnum>0 AND NOT a.attisdropped),
 'constraints', (SELECT json_agg(json_build_array(c.relname,k.conname,
    pg_get_constraintdef(k.oid),k.convalidated,k.condeferrable,k.condeferred))
    FROM pg_constraint k JOIN pg_class c ON c.oid=k.conrelid JOIN pg_namespace n ON n.oid=c.relnamespace
    WHERE n.nspname='public' AND c.relname IN ('conversations','messages','user_ai_integrations')),
 'indexes', (SELECT json_agg(json_build_array(tablename,indexname,indexdef)) FROM pg_indexes
    WHERE schemaname='public' AND tablename IN ('conversations','messages','user_ai_integrations')),
 'policies', (SELECT json_agg(json_build_array(tablename,policyname,cmd,roles,permissive,qual,with_check))
    FROM pg_policies WHERE schemaname='public'
    AND tablename IN ('conversations','messages','user_ai_integrations')),
 'functions', (SELECT json_agg(json_build_array(p.proname,p.prosecdef,p.proconfig,p.prosrc))
    FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace WHERE n.nspname='public'
    AND p.proname='set_philomath_updated_at'),
 'triggers', (SELECT json_agg(json_build_array(c.relname,t.tgname,pg_get_triggerdef(t.oid),t.tgenabled))
    FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid JOIN pg_namespace n ON n.oid=c.relnamespace
    WHERE n.nspname='public' AND NOT t.tgisinternal
    AND c.relname IN ('conversations','messages','user_ai_integrations'))
);
"""


def normalized(value):
    return re.sub(r"\s+", "", value or "").replace("public.", "")


def verify_baseline(snapshot):
    """Compare every exported column, constraint, index, policy and trigger.

    Does not claim to reconstruct absent role/default/PUBLIC metadata.
    """
    exports = json.loads(sql("schema_metadata.json"))["exports"]
    original = exports["Supabase Snippet Untitled query.csv"]["rows"]
    fourth = exports["4.csv"]["rows"]
    expected_columns = []
    for row in original:
        if row["object_type"] != "COLUMN":
            continue
        definition = row["definition"].split(" ", 1)[1]
        pieces = definition.split(" DEFAULT ", 1)
        datatype = pieces[0].replace(" NOT NULL", "")
        expected_columns.append((row["table_name"], row["object_name"], datatype,
            " NOT NULL" in pieces[0], pieces[1] if len(pieces) > 1 else None))
    if sorted(map(tuple, snapshot.get("columns") or [])) != sorted(expected_columns):
        raise ValueError("Baseline columns differ from supplied metadata")
    expected_constraints = {(r["object_table"], r["object_name"]): normalized(r["definition"].split(" | ")[0])
        for r in fourth if r["category"] == "CONSTRAINT"}
    actual = {(r[0], r[1]): normalized(r[2]) for r in snapshot.get("constraints") or []}
    if actual != expected_constraints or any(r[3:] != [True, False, False] for r in snapshot["constraints"]):
        raise ValueError("Baseline constraints differ from supplied metadata")
    expected_indexes = {(r["table_name"], r["object_name"]): normalized(r["definition"])
        for r in original if r["object_type"] == "INDEX"}
    if {(r[0], r[1]): normalized(r[2]) for r in snapshot.get("indexes") or []} != expected_indexes:
        raise ValueError("Baseline indexes differ from supplied metadata")
    expected_policies = {}
    for row in fourth:
        if row["category"] == "RLS_POLICY":
            fields = dict(part.split("=", 1) for part in row["definition"].split(" | "))
            expected_policies[(row["object_table"], row["object_name"])] = (
                fields["command"], ["authenticated"], fields["permissive"],
                normalized(None if fields["using"] == "NULL" else fields["using"]),
                normalized(None if fields["with_check"] == "NULL" else fields["with_check"]),
            )
    actual = {(r[0], r[1]): (r[2], r[3], r[4], normalized(r[5]), normalized(r[6]))
        for r in snapshot.get("policies") or []}
    if actual != expected_policies:
        raise ValueError("Baseline policies differ from supplied metadata")
    expected_triggers = {(r["table_name"], r["object_name"]): normalized(r["definition"])
        for r in original if r["object_type"] == "TRIGGER"}
    actual = {(r[0], r[1]): normalized(r[2]) for r in snapshot.get("triggers") or []}
    if actual != expected_triggers or any(r[3] != "O" for r in snapshot["triggers"]):
        raise ValueError("Baseline triggers differ from supplied metadata")
    functions = snapshot.get("functions") or []
    body = exports["2.csv"]["rows"][0]["definition"].split("$function$")[1]
    if (len(functions) != 1 or functions[0][:2] != ["set_philomath_updated_at", False]
        or functions[0][2] != ['search_path=""'] or normalized(functions[0][3]) != normalized(body)):
        raise ValueError("Baseline trigger function differs from supplied metadata")
