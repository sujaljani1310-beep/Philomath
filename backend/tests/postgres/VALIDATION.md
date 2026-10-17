# Batch 4 validation evidence

Base: `dd328a4b1c0fd2d018da68520c3a636c9eea39b4` on
`philomath-batch4-checkpoint`; the remote checkpoint SHA was also confirmed.
All changes below are uncommitted; no push, merge, deployment, production access
or hosted PostgreSQL execution occurred.

## Completed checks

| Check | Actual result |
|---|---|
| Full socket-blocked regression suite | 310 passed, 0 failed/errors/skipped; 12.386s |
| Real offline PostgreSQL 17.6 suite | 35 passed, 0 failed/errors/skipped; 15.281s |
| Independent full regression run | 310 passed, 0 failed/errors/skipped; 9.633s |
| Independent real local PostgreSQL run | 35 passed, 0 failed/errors/skipped; 14.398s |
| Corrected cleanup fixture repeated | 100/100 passed; 0.388s |
| Focused package/hosted mocks/scaffolding/cleanup | 30 passed; 0.054s |
| Historical planner/scaffold checks after text correction | 10 passed; 0.010s |
| Entire OpenAPI vs checkpoint | Identical: 7 paths, 5 schemas |
| Frontend, app runtime and active migration hashes | Byte-identical to checkpoint |
| Whitespace/diff checks | Passed |
| Secret/artifact scan of changes | No runtime secret, private-key/JWT, .env or dump findings |
| Offline fixture containers after runs | None remaining |
| Genuine hosted PostgreSQL/RLS/Auth/PostgREST tests | NOT RUN |
| Hosted baseline or hardening migrations executed | NONE |

These counts are separate suites/repetitions, not additive new coverage. The
310-test regression suite includes the previous 290 cases plus 20 package/mock
checks. The 35 SQL tests run real PostgreSQL with a synthetic Auth model, not a
Supabase project. Local baseline + unchanged 001 + candidates 002/003 were applied
in a disposable container; unsafe preflight variants rolled back atomically.

An earlier independent regression run reproduced the inherited cleanup-fixture
race (304/305 passed). Its test-only barrier correction was independently verified.
One auxiliary unittest command launched from the repository root failed to import
`tests`; the documented backend-directory command then passed all 10 cases. This
was an invocation error, not an additional application regression.

## Security evidence and limits

The known-policy legacy child was visible to its message owner before hardening
(local count 1) and hidden after requiring parent ownership (local count 0).
Migrations preserve a row-content fingerprint. Both insert/delete orderings,
backend ownership validation, RLS isolation, effective column/table grants,
TRUNCATE denial, disabled-FK gates, NOINHERIT membership rejection, valid/blocked
Auth-stub cascades, upsert identity, timeout rollback/recovery and catalog drift
are exercised. EXPLAIN establishes child-index eligibility only. No production
latency improvement, hosting cold-start removal, vulnerability scan, exactly-once
messages or atomic multi-HTTP persistence is claimed.

Local test_23 discards a successful response and probes a conversation PK conflict;
it is not an actual network fault. Existing Batch 3 synthetic HTTP fault tests
remain in the full suite. Local test_28 explicitly demonstrates duplicate messages
from independent repeated inserts with generated UUIDs. Provider fallback after
persistence errors remains prohibited by the preserved earlier regressions.

## Independent review

Verdict: **READY FOR HOSTED STAGING VALIDATION of code/package only; NOT READY FOR
BATCH 4 SIGN-OFF.** No new Critical/High regression remains in the reviewed changes.
Read-only reviewer reproduced/followed fixes for weak catalog checks, catalog-check
race, latent memberships, backend privileges, disabled FK triggers, local runner
readiness/cleanup and the inherited timing fixture. No reviewer remote access.

## Remaining blockers

There is no callable Supabase/SQL connector or configured management API token in
this Codex environment. Project service-role/public keys do not provide arbitrary
SQL, and REST is not treated as SQL. Raw database connectivity was unavailable;
no repeat configuration loop, tunnel, proxy bypass or exposed SQL RPC was added.
The runnable guarded staging and hosted verification commands are in README.md.

Real staging Auth/PostgREST/SQL integration, schema-cache handling, service-level
legacy/race/fault cases, actual role/effective grants and custom Auth hooks remain
unverified. Missing production security-context metadata must be supplied before
any production rollout. The new composite FK deliberately remains NOT VALID;
legacy-dependent deletes can fail to preserve NULL records. Parent protection
is not universal immutability of every non-NULL inconsistent message. Existing
partial persistence and independently repeated AI requests remain limitations.

## Exact file inventory for this follow-up

- `backend/BATCH4.md`
- `backend/PERFORMANCE.md`
- `backend/tests/postgres/README.md`
- `backend/tests/postgres/VALIDATION.md`
- `backend/tests/postgres/__init__.py`
- `backend/tests/postgres/baseline.sql`
- `backend/tests/postgres/baseline_grants.sql`
- `backend/tests/postgres/bootstrap_local.sql`
- `backend/tests/postgres/catalog_contract.sql`
- `backend/tests/postgres/hosted.py`
- `backend/tests/postgres/local.py`
- `backend/tests/postgres/package.py`
- `backend/tests/postgres/preflight.sql`
- `backend/tests/postgres/requirements.txt`
- `backend/tests/postgres/schema_metadata.json`
- `backend/tests/postgres/staging.py`
- `backend/tests/postgres/target_guard.sql`
- `backend/tests/postgres/two_user_permissions.sql`
- `backend/tests/postgres_plan.py`
- `backend/tests/run_offline.py`
- `backend/tests/test_hosted_verification.py`
- `backend/tests/test_postgres_package.py`
- `backend/tests/test_postgres_scaffold.py`
- `backend/tests/test_request_limits.py`
- `supabase/drafts/002_backend_only_writes.sql.draft`
- `supabase/drafts/003_parent_owner_integrity.sql.draft`
