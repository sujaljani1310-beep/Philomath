# Approved Batch 3: persistence correctness

Implemented in the isolated `work` checkout, with no commit, deployment,
production access, migration, dependency change or frontend change. This document
supersedes the deferred Batch 3 items in the historical sections of
[PERFORMANCE.md](PERFORMANCE.md). Batch 4 and Batch 5 have not been implemented.

## What changed

Basic and Auto still use their existing provider orders and weak-answer policy.
Manual still uses only the selected provider. Provider failures can still cause
the existing fallback or provider-error answer. Once an answer is accepted,
message writes and counting run **outside** provider failure handling. A database
failure cannot invoke another provider or restart the save operation. Database
preparation and history failures also return 503 rather than becoming provider
failures or fabricated provider-error messages.

`PersistenceUnavailable` has one sanitized HTTP 503 body:

```json
{"detail":"The database operation could not be confirmed. Reload to check the current state before trying again."}
```

It includes neither the generated answer nor a `saved_messages` claim. Upstream
error bodies, query text, credentials and exception text are not returned by
these new failure paths. Successful response fields are unchanged. Existing
authentication and Batch 2 input, rate, resource and deadline policies remain.

### Acknowledgments and ownership

- Conversation creation and timestamp updates require exactly one affected row.
  They retain the owner-filtered existence check. Creation is an insert, never
  an upsert on a globally unique conversation ID, so a foreign-ID conflict cannot
  transfer ownership or modify another user's conversation.
- Each message retains its owner-filtered conversation check and requires an
  exact affected-row count of one. Malformed ownership/history results fail
  closed. History still preserves the existing order, request-local snapshot,
  user/conversation/limit isolation and defensive copies. A possibly committed
  insert invalidates that request's history even when its response is lost.
- The response's `saved_messages` is the exact, owner-filtered HEAD count. An
  absent, invalid or implausibly small count fails the request; zero is not
  invented. HEAD does not return message bodies.
- Integration save is one atomic upsert on the **existing**
  `(user_id, provider)` unique constraint in
  [001_google_auth_byok.sql](../supabase/migrations/001_google_auth_byok.sql).
  It returns success only for one acknowledged metadata row whose owner,
  provider, display name, last four characters and nonempty ID match the request.
  No read-then-insert race, fallback payload or fabricated null ID remains.
  Fernet encryption and key normalization remain unchanged; ciphertext and full
  keys are neither selected in the acknowledgment nor returned.
- Deletes retain owner filters and exact affected counts. On the normal,
  acknowledged path, read-only HEAD checks confirm the user's resource is absent
  before returning the existing successful response. An already-absent resource
  can be acknowledged only after this check. A concurrent external replacement
  is not deleted again merely to force a successful acknowledgment.

Every mutation explicitly disables PostgREST retries. Confirmation HEADs also
have no retries. If a write throws, its status is treated as uncertain: there is
no write replay, compensating write, rollback attempt or optimistic success.
Existing safe transport controls and read-only retry policies remain intact.

A fixed array of 256 reentrant locks coordinates related mutations **within one
app process**, with at most one second spent acquiring a lock before sanitized
503. Conversation keys include `(user_id, conversation_id)` at every guard,
including the whole message-pair/count operation; integration keys include
`(user_id, provider)`. Locks release on every exception. The array retains no
user IDs, API keys or growing registry. Hash collisions can incidentally serialize
unrelated requests; these locks are neither a database transaction nor a
distributed lock. Existing Batch 2 admission permits continue to bound work.

## Generated answers, ambiguous writes and recovery

The database protocol cannot distinguish an uncommitted write from a committed
write whose reply was lost. Both return 503 and stop; neither triggers another
generation or an automatic database write retry.

| Failure point | Possible database state | Recovery using existing APIs |
| --- | --- | --- |
| Conversation preparation/key/history read | Conversation may have been created or timestamped; no accepted answer saved | Reload; retry only as a new deliberate request |
| User-message insert | No new messages, or the user message alone | Reload conversation; generated answer is not durably available |
| Assistant-message insert | User message alone, or both messages | Reload; answer is recoverable only if the assistant insert committed |
| Final count | Both messages may already be saved | Reload conversation; no false success is returned |
| Conversation delete | Neither, part, or all of the deletion may have committed | Reload to inspect; a later deliberate delete can confirm absence |
| Integration upsert/delete | Previous or requested state may be present | Reload metadata; a deliberate repeated upsert uses the same existing unique row |

There is no new answer cache, journal or recovery endpoint. If the assistant
message never committed, the generated answer cannot be recovered by this
implementation and may not appear in the unchanged frontend. A 503 does **not**
mean nothing was saved. It means persistence was not confirmed.

Two message inserts and a count are still separate database operations. So are
the two conversation deletes and their confirmation reads. Partial commits
remain possible. Locks prevent some same-process interleavings, but not another
worker, replica, direct database writer or user action after the confirmation.

No idempotency key or durable request ledger was added. Explicitly resubmitting
the same chat can generate another answer and save another pair, including
after an earlier 503. This is reproduced in the regression suite. Content-based
deduplication would incorrectly suppress legitimate repeated messages, and an
in-memory cache cannot guarantee correctness after restart or across workers.
Client-supplied request identity and durable idempotency/transaction design need
separate approval; there is **no exactly-once guarantee**. Provider transport
failure can also be ambiguous, and the approved provider-failure/weak-answer
fallback semantics have deliberately not been changed.

## Exact Batch 3 file inventory

Relative to the approved Batch 2 fingerprint (168 files), only these files changed
or were added. Earlier uncommitted transport/Batch 1/Batch 2 files are preserved.

| File | Change |
| --- | --- |
| `app/services/persistence.py` (new) | Sanitized database failure boundary, exact acknowledgments, bounded mutation guards |
| `app/services/conversation_service.py` | Owner-checked confirmed writes/deletes, history validation/invalidation, strict HEAD counts |
| `app/services/integration_service.py` | Atomic encrypted integration upsert with validated acknowledgment, confirmed owned deletes, preparation failure isolation |
| `app/routes/chat.py` | Separate generation from persistence in Basic/Manual/Auto; protect message pair and count |
| `app/routes/conversations.py` | Unconfigured deletion returns 503 instead of a false successful HTTP acknowledgment |
| `tests/test_database_services.py` | Update four earlier fixtures/assertions for required exact counts and approved 503 behavior |
| `tests/synthetic_database.py` (new) | In-memory PostgREST peer used through real installed SDKs; before/after-commit fault hooks |
| `tests/test_chat_persistence.py` (new) | 16 chat failure/fallback/recovery/ambiguity tests, with mode and malformed-result subcases |
| `tests/test_mutation_acknowledgments.py` (new) | 24 acknowledgment/ownership/concurrency/ambiguous-mutation tests |
| `PERSISTENCE.md` (new) | Current implementation, evidence, recovery semantics and limitations |
| `PERFORMANCE.md` | Current Batch 3 status above historical implementation records |

## Validation and independent review

Run from `backend/` using the existing environment:

```sh
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -B -m tests.run_offline
```

The runner disables dotenv, removes credential environment bindings and blocks
sockets. No dependencies were installed and no external database or provider was
contacted. Fault tests use synthetic data and keys over `httpx.MockTransport`
with the real installed Supabase/PostgREST SDKs. The peer models row mutations
and protocol acknowledgments; it does **not** validate PostgreSQL transaction
isolation, RLS, grants, triggers or a hosted PostgREST instance.

**260 tests passed, 0 failed, 0 skipped, in 7.839 seconds.** All 220 earlier tests
remain, with the four database fixtures/assertions updated for the approved
failure policy; 40 new tests contain additional mode/failure subcases. Coverage
includes failures before/after each message write, commits with lost replies,
count failures after both writes, provider fallback, malformed history and
ownership results, encrypted integration upserts, failed/partial deletes,
repeated submissions, concurrent mutations, lock saturation/recovery, user
isolation and sanitized actual HTTP responses. Prior transport, provider,
ingress, extraction, cancellation, logging, dependency and startup regressions
also pass.

The **complete OpenAPI document** equals the saved approved Batch 2 baseline:
7 paths, 5 component schemas, no differences. This includes all operations,
parameters, request bodies, responses and security declarations. The approved
new 503 failure behavior remains represented by the existing generic FastAPI
HTTP exception contract; no new success fields or OpenAPI declarations were
introduced. Exact OpenAPI equality alone does not prove runtime compatibility;
the HTTP/route regressions check representative successful and failed paths.

Measured fault-injection results: each accepted-answer persistence failure in
Basic, Manual and Auto invokes exactly one generation chain entry, makes no
write replay and returns 503. A second-message lost reply leaves two committed
rows in the peer, not four; the explicit resubmission test deliberately then
produces four. These are correctness measurements, **not** claimed production
latency improvements or database throughput benchmarks. Normal integration
save uses one POST instead of the old lookup then write. Confirmed conversation
deletion adds two HEAD reads to its two deletes, an intentional correctness cost.

A fresh independent read-only reviewer independently passed all **260 tests in
7.730 seconds**, compared the full OpenAPI document, checked preservation
fingerprints and reviewed all Batch 3 implementation and new regression tests.
Two medium findings were corrected and independently reproduced as closed:

1. Conversation-ID-only locks permitted targeted cross-user contention. All
   guards now use user plus conversation identity; the overlap regression covers
   all four conversation mutation paths.
2. Malformed history processing outside the database boundary could trigger
   provider fallback and persist an error answer. History shape validation and
   materialization now fail closed inside that boundary; malformed ownership
   acknowledgments also cannot authorize writes.

**Independent verdict: READY FOR STAGING, Batch 3 only. No unresolved Batch 3
blocking finding. Overall production release: NEEDS FIXES.**

## Remaining blockers and safe validation before deployment

- Batch 4 database/RLS/grant and parent-ownership/atomicity findings remain
  unresolved. Application owner filters and process locks do not prove actual
  database policy enforcement or eliminate cross-process check/write races.
- Batch 5 inherited exception handling outside these database paths remains;
  for example the outer generic chat error response still reflects exception
  text. This batch does not claim a complete error-sanitization audit or fix.
- Real hosted PostgREST must honor exact mutation counts/HEAD headers and return
  the requested integration projection. Missing acknowledgments safely produce
  503, but could cause availability failures. Verify only on a separately
  approved disposable staging database with synthetic accounts, the existing
  unique constraint, triggers and actual configured grants. No such service
  validation was performed here.
- Exercise multi-worker mutations, real RLS, lost replies at a proxy, partial
  message pairs and frontend reload/recovery in that approved staging setup.
  Confirm stable integration IDs/creation timestamps and owner isolation on
  conflict updates. Do not test destructive faults against production.
- Prior Batch 2 limits remain: per-process resource/rate budgets, synchronous
  upstream inactivity timeouts rather than a total chat deadline, slow-drip
  upstream risk, document-worker hosting resource assumptions, and provider
  context/token compatibility requiring representative staging tests.
- No dependency vulnerability scan or live provider compatibility test was
  performed in this batch. Existing pins, encryption and all provider transports
  remain unchanged. Code cannot eliminate Render Free cold starts.

No Batch 4/5 work has begun. Review and approval are required before further
implementation or any deployment.
