# PostgreSQL/RLS verification scaffolding — BLOCKED, NOT EXECUTED

No database execution, local or remote, is approved in this Batch 4 step. No
PostgreSQL driver was added. The Python planner never connects, launches database
tools, accepts a DSN, applies SQL or reads environment credentials. It has no
execution option. All SQL scaffolds and migration drafts fail unconditionally
before any table operation and end in ROLLBACK. Do not remove these guards.

Safe offline command (only prints a plan):

```
.venv/bin/python -B -m tests.postgres_plan
```

Already supplied sanitized metadata can optionally be inspected via `--metadata`
with a local JSON file. Required evidence keys are listed by the planner; empty
sections must reflect explicit catalog evidence, not invented missing objects.
Even complete keys produce REVIEW_REQUIRED, never a verified-schema verdict.

Before actual integration tests, obtain explicit approval, a disposable local
PostgreSQL/Supabase fixture, reviewed canonical core-table DDL, genuine synthetic
Auth UUIDs A/B, role/auth.uid() bootstrap, and complete policies/grants/functions.
Do not assume a plain PostgreSQL installation contains Supabase's managed roles
or auth schema. Do not copy real Auth users or data into fixtures. Default tools
must not discover a target from DATABASE_URL, PGHOST or Supabase environment.

SQL files here are draft test recipes, not runnable baseline migrations. Extra
NOT NULL columns, FK actions and triggers are unknown. catalog_contract.sql
defines necessary effective privilege checks; two_user_permissions.sql outlines
JWT-identity/role tests in a rollback-only transaction. Complete both directions,
anonymous tests, direct INSERT/UPDATE/DELETE, forged parent/owner cases and
unchanged-data assertions after canonical metadata is supplied. Service-role
success must not be used as evidence that authenticated RLS works.

The future matrix must cover: baseline vs hardened privileges; legacy NULL
parents/children retained in place; owner-aware FK enforcement even for backend
roles; exact PostgREST write counts and HEAD; metadata-only stable-ID upserts;
separate-worker inserts/deletes; lost write replies without replay; transaction
rollback and schema-cache refresh. PostgreSQL tests alone do not test PostgREST
headers or actual Supabase Auth. These need separately approved service tests.

Never apply draft 002/003 to produce the baseline. The baseline needs canonical
pre-001 core tables plus unchanged migration 001, or a verified equivalent full
snapshot. Preserve all existing deletion semantics and integration uniqueness.
