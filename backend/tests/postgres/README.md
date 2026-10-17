# Batch 4 database execution and verification package

## Focused read-only connection diagnostic (including macOS)

From `backend/`, using the same Python environment as the failing connection:

```sh
python -B -m tests.postgres.diagnose
```

The diagnostic reads the existing three credential bindings and the optional
`PHILOMATH_STAGING_CA_FILE` setting described below, reuses
the staging/production and ambient-PG* guards, and prints sanitized JSON. It does
not accept a DSN on the command line, print exception messages, run migrations or
query user records. It first verifies the staging API using certifi, then tests
TCP, PostgreSQL SSLRequest and hostname-verified TLS with no password sent by that
probe. It reports the loaded libpq version/implementation and compares three
authenticated read-only profiles: minimal startup, the runner's startup options,
and the runner's CA path/options. With no explicit CA, these use certifi for the
first two profiles and the runner's Python-default CA path for the third. With an
explicit CA, every PostgreSQL profile and the TLS probe use that file; the third
profile is named `libpq_configured_runner_ca`, with no default-CA fallback.
Every profile retains `sslmode=verify-full`. Only session identity/read-only
state is queried. These are connection probes, not write retries.

Interpret the first failing phase/category:

- `tcp_connect`/`network`: DNS, refusal, timeout or reachability.
- `postgres_tls`/`tls`: PostgreSQL certificate/trust/hostname negotiation.
- `libpq_load`/`libpq`: missing/unloadable client wrapper or architecture mismatch.
- `libpq_minimal_connection`/`authentication`: PostgreSQL rejected authentication
  or access; SQLSTATE `28P01` is invalid-password authentication, not a TLS error.
- `libpq_runner_startup_options_connection`/`startup_configuration`: minimal
  login worked but the original startup options were explicitly rejected.
- `libpq_existing_runner_default_ca_connection`/`tls`: certifi profiles worked
  while the existing default CA configuration failed. HTTPS using certifi alone
  never establishes what libpq's default CA settings will do.

Pooler routing, connection limits and upstream availability have separate safe
categories. Unclassified errors remain explicitly unclassified; a connection
close without SQLSTATE is not proof of a wrong password. A full PASS confirms
these session checks only, not hosted schema/RLS validation. Do not share raw
tracebacks, environment values, connection strings or passwords.

## Explicit PostgreSQL CA certificate (including macOS)

An official CA downloaded from the trusted staging Supabase dashboard can be
configured without modifying certifi, the macOS Keychain, connection strings or
repository files. Keep the certificate outside the repository. From `backend/`,
in the Python environment where psycopg is already installed, with the three
existing staging credentials privately configured:

```sh
PHILOMATH_STAGING_CA_FILE="$HOME/.config/philomath/certs/prod-ca-2021.crt" \
  python3 -B -m tests.postgres.staging --action inspect
```

This command only inspects schema metadata in a read-only transaction. The
certificate's filename does not select a project: the fixed staging-only target
and production rejection remain enforced. The same setting works with
`tests.postgres.diagnose` and `tests.postgres.hosted`; the hosted verifier still
creates/deletes synthetic fixtures and is not a read-only command.

The tool expands a literal `~`, resolves the path, and requires a readable PEM
certificate file. Blank, missing, unreadable or malformed configured files fail
before any network request. A validation/handshake failure is never retried with
another CA or weaker TLS. Parsing does not prove provenance: trust only a CA
obtained through the official dashboard, never a certificate copied from a
failed/unverified connection. Do not set `PGSSLROOTCERT`: ambient `PG*` overrides
remain forbidden. If the new setting is absent, PostgreSQL defaults are unchanged.

All libpq sessions retain `sslmode=verify-full` and the original verified host.
The diagnostic TLS probe retains hostname and certificate verification. HTTPS
Auth/REST calls use certifi independently; the PostgreSQL CA is never added to
HTTPS trust, and credential redirects remain forbidden. No system trust store or
certificate bundle is modified, and paths/certificate contents are not reported.

Checkpoint: `dd328a4`. Production is forbidden; the sole remote target is
`tdiqzmaqvoynspmilwra`. No hosted SQL has been executed. Local PostgreSQL results
are not evidence of Supabase Auth, PostgREST or the production security context.

## What is provided

- `schema_metadata.json`: all four sanitized exports, original row counts and
  SHA256 provenance. No records, passwords or API keys. Unknown privilege/context
  properties are listed explicitly, not filled with guessed production defaults.
- `baseline.sql` plus unchanged `supabase/migrations/001_google_auth_byok.sql`:
  all 22 known columns, nine constraints, seven indexes, seven policies and the
  timestamp trigger/function. `baseline_grants.sql` reproduces the 70 known table
  grant rows. Column order and missing PUBLIC/inherited/default/schema ACLs are
  not claimed identical to production. Supabase owns its Auth schema and roles;
  the remote baseline never creates/replaces them.
- Candidate 002 revokes PUBLIC/browser table AND column privileges, removes
  browser DML policies, preserves own SELECT and requires an owned message parent.
  It also revokes public-schema CREATE and fails on unsafe inherited roles,
  exposed views, privileged RPCs, or missing backend CRUD/BYPASSRLS capabilities.
- Candidate 003 adds the reviewed parent-owner UNIQUE/FK and child lookup index,
  freezes ownership, locks message parents, and protects NULL/inconsistent legacy
  records. Original FKs remain CASCADE/NO ACTION/NOT DEFERRABLE. The new composite
  FK intentionally remains NOT VALID: pre-existing inconsistent records stay in
  place; this is not a fully validated invariant over all historical data.
- `staging.py`: fixed staging-only inspect/baseline/harden operations, exact known
  catalog comparison, TLS verify-full, bounded locks/statements, no credential
  logging and no automatic write/commit retries. Both hardening candidates and
  postconditions run in one transaction. Exact comparison repeats under table
  locks to close the catalog-check/application race.
- `hosted.py`: later genuine two-user Auth/REST/SQL checks with fresh tagged
  synthetic users, encrypted dummy integration keys, own/foreign reads, browser
  mutation denials, backend parent rejection, concurrent upsert identity, valid
  cascade and scoped cleanup. Passwords/tokens stay in memory. Run ID and failure
  categories alone are printed. A lost create response may leave a tagged user;
  report/inspect that run before manual recovery, never replay blindly.

## Verified offline workflow in Codex Cloud

From `backend/`, with Docker available:

```sh
# Explicit initial tooling download; subsequent SQL tests use --pull=never.
docker pull postgres:17.6-bookworm@sha256:f3bd19c606e442c3d7bdfa8002e03fe260a1023351e0ea4598032022b68dd6e3
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -B -m tests.run_offline
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -B -m tests.postgres.local
```

The local server has `--network none`, no published ports or host mounts, 512MiB
memory, one CPU and a 160MiB tmpfs database. Synthetic role/auth.uid() definitions
in `bootstrap_local.sql` model RLS; they are not real Supabase Auth. Containers
have unique names, a `philomath.batch4=offline-only` label, `--rm`, and finally /
SIGTERM cleanup. SIGKILL/host failure cannot run finally: inspect only containers
with that label after an interrupted run and remove the confirmed test instance.
No staging credentials are read by the local runner. SQL uses container-local
Unix sockets. Readiness requires the final postgres PID 1 and pg_isready.

Local tests reproduce the legacy read defect, effective table/column privileges,
TRUNCATE denials, NULL/mismatched-parent writes, immutable ownership, original FK
clauses, valid and blocked Auth-stub cascades, preservation fingerprints, both
insert/delete lock orderings, concurrent upserts, transaction/timeout rollback,
missing execution guards, catalog drift and disabled-constraint-trigger gates.
They explicitly demonstrate that separate message submissions still duplicate.

## Later staging execution, with PostgreSQL connectivity

Do not run these commands until the staging target and access are approved.
No IDE is required. Keep all values in private environment bindings; never put
connection strings or keys in arguments, repository files, shell history or logs.
Use the three credential bindings and, when needed, `PHILOMATH_STAGING_CA_FILE`.
The runner rejects ambient
PG* settings (especially PGSERVICE, PGHOSTADDR and PGOPTIONS) so they cannot
silently override the approved host or disable constraint enforcement.

Optional driver installation stays outside backend dependencies:

```sh
.venv/bin/python -m pip install --target /tmp/philomath-batch4-driver -r tests/postgres/requirements.txt
PYTHONPATH=/tmp/philomath-batch4-driver .venv/bin/python -B -m tests.postgres.staging --action inspect
```

Review the actual schema first. Stop for partial application tables, unknown
objects/policies/roles/triggers, incompatible constraints, disabled FK triggers,
unsafe memberships/views/privileged RPCs, or unverified metadata. `inspect` prints
counts, not function bodies or credentials. A project-qualified pooler login (or
exact direct hostname), verified TLS, fixed staging API origin and accepted API
keys are provider-routing identity evidence, not a database-signed project ID.
The SQL GUC guard is an additional execution acknowledgement, not identity proof.

For an empty application schema only:

```sh
PYTHONPATH=/tmp/philomath-batch4-driver .venv/bin/python -B -m tests.postgres.staging --action baseline
PYTHONPATH=/tmp/philomath-batch4-driver .venv/bin/python -B -m tests.postgres.staging --action harden
PYTHONPATH=/tmp/philomath-batch4-driver .venv/bin/python -B -m tests.postgres.hosted
```

For existing tables, skip baseline and require exact known-catalog comparison
before harden. Never force, reset, drop, backfill, infer legacy owners, remove
original FKs or copy production records. Candidate files deliberately stay in
`supabase/drafts`, outside deployment discovery. Do not run them by clipboard
without the identity checks/guard/transaction. Application service-role keys
cannot authorize arbitrary SQL; there is no SQL-execution REST endpoint here.
An authorized project-scoped Management API/MCP integration could execute the
same reviewed artifacts later, but none is connected or credentialed in this
Codex session. No network policy or credential configuration was changed.

A lock timeout/failure rolls back the hardening transaction. A lost commit reply
is ambiguous: inspect schema/state before rerunning. Never automatically retry a
DDL or DML request after uncertainty. No script promises exactly-once delivery.
Hosted Auth email/password sign-in must already be permitted for synthetic test
accounts; the verifier never changes Auth/provider/dashboard configuration.

## Remaining sign-off checks

Actual hosted tests have NOT run. A later run must retain reports for real Auth
JWTs, PostgREST HEAD/count/return preferences and schema-cache reload behavior,
parent/delete/upsert races through the real service, and legacy NULL/mismatch
fixtures seeded before hardening. The automated hosted verifier covers the main
Auth/REST/SQL matrix; local.py is the executable full legacy/race matrix. Port that
matrix with synthetic Auth UUIDs if hosted legacy/race tests are required; the
local Auth stub must never be substituted for a hosted test result.

Before production review, obtain schema-only evidence for missing PUBLIC/column /
inherited/default grants, schema permissions, memberships/role attributes,
FORCE RLS, index validity, function ownership/EXECUTE, exposed views/RPCs,
event triggers and PostgreSQL/PostgREST versions. The candidate intentionally
fails unknown unsafe state rather than silently revoking unrelated roles.
Also inventory custom Auth-schema triggers/hooks that reference application
tables, or confirm there are none; staging hooks do not prove production hooks.

Deletion is intentionally stricter for legacy data: parent deletion with NULL /
foreign children, and any NULL-owned message deletion, fails atomically. An
Auth-user cascade encountering such a guard also fails. Non-NULL inconsistent
messages are not universally frozen against every Auth-user cascade; their
original user FK behavior remains. Valid cascades continue. This needs explicit
production rollout review, as do migration lock time/large-index costs and the
new NOT VALID constraint. Existing FastAPI HTTP guards still add round trips;
no production latency improvement is claimed. Partial multi-request persistence
and independently resent AI requests remain Batch 3 limitations, not solved by
these constraints. No hosted staging sign-off or production readiness claim.
