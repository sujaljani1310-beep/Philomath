# Batch 4 — prepared and verified offline; hosted validation outstanding

The preserved GitHub checkpoint is `dd328a4b1c0fd2d018da68520c3a636c9eea39b4`
on `philomath-batch4-checkpoint`. This follow-up is uncommitted. Batches 1–3 and
all checkpoint application safeguards remain intact; no frontend, provider,
public API model, runtime dependency, active migration or deployment file changed.
No commit, push, merge, deployment or Batch 5 work was performed.

## Implemented follow-up

- Reconstructed the known schema from all four user-supplied sanitized metadata
  CSVs, with provenance, rather than inventing missing definitions. See
  `tests/postgres/schema_metadata.json`. All 22 columns, nine original constraints,
  seven indexes/policies, timestamp trigger/function match the local catalog.
- Candidate 002 removes browser/PUBLIC table and independent column privileges,
  including TRUNCATE/REFERENCES/TRIGGER, preserves authenticated own reads, removes
  browser write policies, and adds an owned-parent condition to message reads.
  Effective denials and preserved backend CRUD/BYPASSRLS are asserted.
- Candidate 003 adds parent-owner uniqueness, a NOT VALID composite FK retaining
  CASCADE/NO ACTION/NOT DEFERRABLE, and a child lookup index. Invoker functions
  with empty search_path enforce future owned parents/messages and immutable
  owners, lock exact parents for message writes, and reject destructive legacy
  cascades. Existing legacy rows are neither repaired nor rewritten.
- Added guarded local/staging runners and genuine hosted verification code.
  Remote execution accepts only the staging reference, rejects production and
  ambient libpq overrides, verifies TLS/API, uses bounded deadlines and table
  locks, compares exact known metadata under locks, and never retries writes.
- Added offline safety/hosted-flow mocks and actual local PostgreSQL regressions.
  Added staging credential removal to the existing socket-blocked offline runner.
- The independent reviewer reproduced the earlier timing-fixture race under
  contention. The cleanup test now uses direct request-limit middleware plus an
  app-entered barrier and controlled expiry; integrated ingress deadline tests
  remain. Production request deadlines/runtime middleware did not change.

## Security and compatibility limits

Original FK clauses and all successful API schemas remain. Effective deletion
outcomes are intentionally stricter: inconsistent/NULL children block parent
deletion; NULL message deletion is blocked. Auth-user cascades encountering a
guard fail atomically. A non-NULL inconsistent message is not protected against
every Auth-user cascade. Normal valid-row cascades still pass.
The composite FK is deliberately NOT VALID and does not certify all historical
rows. New triggers protect future writes and preserve legacy NULL ownership.

All original backend application checks stay in place. Extra HTTP metadata reads
from the checkpoint remain (first history 3 vs prior 1; delete 9 vs prior 4).
The new child index supports parent lookups; index eligibility is tested, not a
production speed measurement. Additional constraints/triggers have write costs.
There is no claim of eliminating cold starts, exactly-once messages, atomic
multi-HTTP chat saves/deletes, or reducing production latency.

## Actual versus unavailable capabilities

No production connection or data access occurred. Prior staging API credential
checks returned 200; raw PostgreSQL failed before handshake due runtime DNS /
network constraints. This follow-up did not repeat that configuration loop.
Tool discovery exposed no Supabase/SQL integration, and no management API token
binding is available. App service-role/public keys are not arbitrary SQL access.
No endpoint, SQL RPC, tunnel, network bypass or application credential was added.

Local tests use real PostgreSQL 17.6 in a digest-pinned Docker container with
external networking disabled and synthetic Auth/roles. They are not hosted
Supabase Auth/PostgREST or production-compatibility validation. No schema creation,
security migration, Auth-user creation or PostgreSQL query ran in hosted staging.

## Validation evidence

Final test counts, timings, complete OpenAPI comparison and independent review
are recorded in `tests/postgres/VALIDATION.md`. Reproducible commands, artifacts,
identity guards, later staging execution and remaining metadata requirements are
in `tests/postgres/README.md`.

## Readiness

Prepared for review and later isolated staging execution. Batch 4 is **NEEDS
FIXES / NOT READY FOR HOSTED STAGING SIGN-OFF** until genuine hosted Auth/RLS /
PostgREST, canonical effective-permission, legacy/race and failure tests pass.
Missing production security-context metadata remains a production-release blocker.
No production migration should be promoted or executed from these candidates.
