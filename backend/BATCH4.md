# Batch 4: backend guards implemented; database work blocked

This work is based on checkpoint `f45529c` on `philomath-hardening`. No database
was contacted or executed, no staging project was created, no dependencies were
installed, and no commit/push/deployment or environment configuration change was
made. Frontend, provider routing, Supabase Auth, encrypted BYOK handling and
successful API schemas remain unchanged. Batch 5 has not begun.

## Evidence and approved target

The user manually reported production metadata: the three tables exist;
conversation IDs and message parent IDs are TEXT, message IDs are UUID; both
conversation/message owner columns are nullable; separate user/parent FKs exist
but no composite owner constraint was demonstrated; integration owner/provider
uniqueness exists; seven authenticated ownership policies and RLS are present;
authenticated table CRUD grants exist for conversations/messages; anon SELECT
grants exist but no applicable anonymous policies were observed; checked browser
integration privileges are absent; legacy NULL-owned conversations exist.

These are **user-reported observations, not independently verified PostgreSQL
or staging results**. No extra permissive deployed policy is assumed. Policy
expressions, effective grants, constraints and triggers still need full evidence.

Approved target: FastAPI performs all application writes, authenticated own-row
conversation/message reads remain available, integrations remain backend-only,
legacy NULL records stay in place, and existing FK delete behavior is preserved.
**Browser write restrictions are not enforced by this code change.** They require
an approved, verified database migration that has not been finalized or applied.

## Implemented application checks

Only `app/services/conversation_service.py` changes runtime application code.

- Message detail reads check the owned parent first; a missing, foreign or
  NULL-owned parent returns the existing empty message-list schema. Valid rows
  retain all original fields. Malformed/mismatched message identity fails 503.
- History and counts require a currently owned parent. History snapshot hits
  also check ownership, retaining one message-data read across provider fallbacks.
  Newly fetched history/count/detail data is checked against the parent again
  before being returned or cached. Changes/outages fail with sanitized 503.
- A successful message insert rechecks its parent. A changed parent rejects
  acknowledgment without replaying the write, rolling it back, or invoking an
  additional provider. The insert may already have committed.
- Delete validates the owned parent before modifying children. It will not
  delete inconsistent caller-owned messages under a foreign/missing parent just
  to manufacture absence. A truly absent caller resource retains idempotent
  success. Detected inconsistent state returns sanitized 503.
- For an owned parent, metadata-only HEAD counts compare all children to owned
  children. NULL/foreign children cause rejection before any delete. A second
  all-child HEAD after deleting owned messages rejects any remaining child,
  and ownership is rechecked before parent deletion. No foreign content is read
  or returned. These cross-owner counts are private safeguards scoped to a
  previously validated parent, not new public count endpoints.
- New parent metadata checks and HEADs explicitly disable SDK retries, including
  Retry-After waits. Batch 3 uncertain-write/no-fallback behavior is retained.

**These are best-effort HTTP checks, not database atomicity or constraints.**
An external writer can still change the parent between requests. A foreign/NULL
child inserted after the final HEAD but before parent DELETE may be affected by
an unknown FK action or trigger. Thus global preservation under concurrency
cannot be certified. No owner-aware FK, transaction, RPC or exactly-once delivery
was implemented. Do not deploy this as a completed database-integrity fix.

The code performs no NULL-owner assignment/backfill/repair or direct NULL-row
mutation. Static detected NULL fixtures remain untouched in tests. Deletion
semantics and concurrency safety in a real database remain blocked on DDL and
future approved atomic design; do not infer safety from the synthetic peer.

## Latency tradeoff

Normal installed-SDK/mock request counts (not production timings):

| Operation | Batch 3 | Current |
| --- | --- | --- |
| First history read | 1 | 3 |
| Cached history access | 0 | 1 |
| Message insert including ownership checks | 2 | 3 |
| Message count | 1 | 3 |
| Successful owned conversation delete | 4 | 9 |

Extra metadata requests improve failure detection but can increase chat, detail
and delete latency. No performance gain is claimed. Each uses existing HTTP
inactivity budgets; there is still no total chat/detail deadline. The two startup
list endpoints and their cancellable total deadlines are untouched. A future
single-query/transaction design needs verified relationships/DDL first.

## Timing-test investigation

The former blocked-504 test used a 50ms total budget with only a 5ms delivery
reserve. Ingress cleanup/thread scheduling can consume that reserve. When it is
exhausted, `_send_error` correctly aborts before scheduling a response, so the
old unconditional `[504]` header assertion could fail.

The blocked-delivery unit test now controls the remaining budget to isolate
transmission cancellation. A new deterministic clock-based test models cleanup
exhaustion, asserts no headers are sent, checks permits/tasks/context are cleared,
and verifies recovery on a new request. Integrated real-deadline tests remain.
The runtime middleware and production ten-second startup deadline did not change.
This reproduces the observed failure mode; the exact original scheduling pause
was not profiled, so no particular host component is proven responsible.

## PostgreSQL scaffolding and blocked migration drafts

`tests/postgres_plan.py` is a **non-executing planner**. It accepts only an optional
bounded local sanitized JSON artifact, hashes it without printing its contents,
and reports missing evidence. It has no driver, target URL, subprocess, credential
discovery or execution option. Even complete evidence keys yield REVIEW_REQUIRED,
not schema verification. Symlinks/nonregular/oversized inputs are rejected.

`tests/postgres/*.sql` are future catalog/two-user permission test recipes. They
do not reconstruct missing baseline DDL. They fail unconditionally before table
operations and end in ROLLBACK. They are not collected/executed by Python tests.
Fixture definitions, symmetric cases and real independent-session races must
be completed after canonical evidence and database execution approval.

Drafts are outside active migration discovery:

- `supabase/drafts/002_backend_only_writes.sql.draft`: proposed table revocations
  and authenticated SELECT preservation, with blocking prerequisites for column
  and inherited grants, exact policies, exposed views/functions, backend role
  privileges and effective permission checks. The table statements alone do not
  establish the target. No Auth-schema grant changes are proposed.
- `supabase/drafts/003_parent_owner_integrity.sql.draft`: design only; no guessed
  constraint/index/trigger DDL, delete action or data repair. It identifies the
  intended parent-owner invariant and required legacy/constraint decisions.

Both have unconditional error guards inside BEGIN/ROLLBACK. They cannot be
applied as delivered, even if a SQL client continues after the error. Do not
remove guards, rename, copy into active migrations or execute them without new
review and explicit approval. The original migration 001 is byte-for-byte intact.

## Canonical evidence required before finalization

1. Complete column definitions, defaults, nullability and timestamp semantics.
2. Full PK/UNIQUE/CHECK and FK definitions, columns, MATCH, ON DELETE/UPDATE,
   deferrability, validation state and dependencies. No delete action is guessed.
3. Full index definitions and validity, including any existing suitable parent
   uniqueness. Do not invent or duplicate a parent index.
4. Trigger and function definitions, security/search-path/execution attributes.
5. Exact policy names/commands/roles/permissive modes/USING/WITH CHECK expressions.
6. Effective table/column/PUBLIC/inherited/default grants, schema access, table
   ownership and browser/backend role attributes; exposed views and write RPCs.
7. PostgreSQL/PostgREST compatibility settings and a separately approved local
   disposable fixture or staging project before real database verification.

No production row export or secret is required for these artifacts. Legacy
NULL records must remain unchanged: do not guess ownership from other rows or
immediately add NOT NULL. NOT VALID FKs/CHECKs have differing semantics and can
still reject updates to existing rows; UNIQUE/NOT NULL require separate review.
Preserve the existing integration UNIQUE(user_id,provider) and FK behavior.

## Validation and independent review

Run from `backend/`:

```
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -B -m tests.run_offline
```

The runner disables dotenv, removes credential bindings in the test subprocess
and blocks sockets. **290 passed, 0 failed, 0 skipped, in 8.520s**: the 260 earlier
tests remain, plus 19 parent-ownership, 10 scaffold-safety and one timing-cleanup
regression. Existing focused tests were updated for new ownership queries and
approved fail-closed cases; no prior no-replay/fallback assertions were removed.
The focused run before the final Retry-After case passed 121 tests. Forty repeated
timing/cleanup cases passed. Complete OpenAPI equals the saved Batch 3 document
(7 paths, 5 schemas; no differences). `git diff --check` passes.

Fresh independent read-only review passed **290 tests in 8.756s** and found no
new critical/high blocking regression. It confirmed the best-effort race limits
and extra request costs. **Review verdict: READY FOR ISOLATED DATABASE VALIDATION
of this code/scaffolding only. Overall production: NEEDS FIXES / canonical
metadata and real database verification.** Actual PostgreSQL, hosted Supabase,
RLS enforcement, migration execution and real provider test coverage: **zero**.

## Exact change inventory

| File | Change |
| --- | --- |
| `app/services/conversation_service.py` | Parent checks, detection of inconsistent children, strict metadata reads |
| `tests/test_database_services.py` | Updated protocol fixtures; history tests isolate caching |
| `tests/test_mutation_acknowledgments.py` | Preserve fault/replay assertions around new preflight requests |
| `tests/test_request_limits.py` | Separate blocked delivery and exhausted-cleanup deadline cases |
| `tests/test_routing_contracts.py` | Count history data reads separately from ownership metadata |
| `tests/test_parent_ownership.py` (new) | 19 ownership/legacy/concurrency/ambiguous-write regressions |
| `tests/test_postgres_scaffold.py` (new) | 10 non-execution/draft safety regressions |
| `tests/postgres_plan.py` (new) | Credential-free non-executing readiness planner |
| `tests/postgres/README.md` (new) | Fixture requirements, limits and approval boundaries |
| `tests/postgres/catalog_contract.sql` (new) | Blocked effective-privilege contract recipe |
| `tests/postgres/two_user_permissions.sql` (new) | Blocked synthetic role/identity test recipe |
| `../supabase/drafts/002_backend_only_writes.sql.draft` (new) | Inert, incomplete permission draft |
| `../supabase/drafts/003_parent_owner_integrity.sql.draft` (new) | Inert constraint design; canonical DDL blocked |
| `BATCH4.md` (new), `PERFORMANCE.md` | Current evidence, blockers and historical status clarification |

Remaining work is not silently implemented: direct database write revocation,
owner-aware constraints, atomic persistence/deletion, legacy ownership treatment,
default conversation-ID behavior, durable idempotency and context-bound key
envelopes. Batch 5 remains deferred. Canonical evidence is required before
finalizing migration SQL. Database execution, staging creation, commits/pushes
and deployment require new approval.
