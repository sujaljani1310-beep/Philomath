# Backend performance changes for review

## Batch 4: backend safeguards and blocked database drafts

Current partial Batch 4 work is documented in [BATCH4.md](BATCH4.md). Parent-owner
checks and metadata-only delete safeguards are implemented and covered by 290
offline tests. They add database round trips and do not establish an atomic
parent-owner invariant. Startup list endpoints and provider behavior are intact.
The timing-test change separates blocked delivery from an already exhausted
cleanup budget; production deadlines are unchanged.

No database was connected or executed. PostgreSQL recipes are blocked scaffolds;
versioned drafts live outside active migrations and fail closed. Browser write
revocation, final constraints and deletion safety remain blocked on canonical
DDL and real database verification. Overall production status remains NEEDS
FIXES; the historical Batch 1–3 verdicts below do not certify Batch 4 database
security. No frontend, active migration, provider, dependency or deployment
configuration changes were made in this batch.

## Approved Batch 3: persistence correctness

Batch 3 is now implemented and documented in [PERSISTENCE.md](PERSISTENCE.md),
which supersedes the deferred Batch 3 statements in the historical sections
below. Accepted answers cannot trigger provider fallback on save/count failure;
uncertain writes stop without replay and return sanitized 503. Mutations require
confirmed acknowledgments and retain owner filters. Successful schemas and
provider-selection semantics are unchanged. Local locks are not transactions;
partial commits and explicit duplicate submissions remain possible.

All **260 offline tests passed** (220 earlier tests plus 40 new tests), and the
complete OpenAPI document exactly matches the approved Batch 2 baseline. A fresh
independent read-only review also passed all 260 tests and returned **READY FOR
STAGING for Batch 3 only**, with no remaining Batch 3 blocker. Overall production
release remains **NEEDS FIXES**; inherited Batch 4 database/RLS and Batch 5 issues
are not addressed. All earlier work is preserved; no frontend, SQL, dependency,
environment or deployment configuration was changed in Batch 3.

The following sections record earlier checkpoints and their then-current limits.

## Approved Batch 2: current implementation and validation

This section supersedes the historical Batch 1/transport-only scope, timeout,
and retry statements below. Batch 2 is implemented in the isolated development
checkout. Batch 3 persistence changes and Batch 4 database policy/schema changes
are **not implemented**. No frontend, environment, migration, schema, deployment,
or production setting was modified; nothing was committed, pushed or deployed.

On resumption, branch `work` remained at `2f13d61`; all implementation work was
uncommitted. Pre-Batch-2 fingerprints confirmed the previous redirect/cookie
protections, proxy/connection management, linear routing scan, nonblocking
logging, database services, encrypted BYOK code and dependency pins were present.
The saved implementation already passed 210 tests. The resumed work completed
fresh validation and review, fixed four additional review findings, and added
ten regressions. No dependencies were installed or upgraded.

### Limits and behavior

| Resource | Default budget |
| --- | --- |
| Upload request body | 21 MiB, actual streamed bytes checked before FastAPI multipart parsing |
| Files | 5 files; 10 MiB each; 20 MiB aggregate; filenames 255 characters |
| Multipart metadata | 10 fields, 4,096 bytes/field, 8,192 raw header bytes/part, 16 headers/part |
| Other request bodies | Chat POST 4 MiB; other API requests 16 KiB; root health request 1 KiB |
| Ingestion time | Upload POST 180 seconds; other bodies 15 seconds; startup GET total deadline still 10 seconds |
| Spooling | 1 MiB in memory, then temporary disk; 64 KiB parsing/I/O/replay chunks |
| Document worker | One process per app process, zero wait on saturation; 10-second wall budget; 6-second soft/7-second hard CPU limit |
| Worker OS resources | 384 MiB address space; 32 descriptors; no file output/core dumps; minimal environment and closed inherited descriptors |
| DOCX | 32 MiB expanded content; 1,000 ZIP entries; reject traversal/encryption/XML entity declarations |
| PDF | 200 pages; 32 MiB aggregate page content and individual decoding budgets; external JBIG2 command disabled |
| Extracted output | 250,000 characters/file; 500,000 aggregate text characters |
| Chat input | 32,000 message characters; 502,000 context characters including upload filename/separator overhead; IDs 200/provider names 80 characters |
| Chat modes | Exact `basic`, `manual`, `auto`; invalid modes rejected before chat service/provider work |
| Provider input | 128,000 assembled Unicode characters including system prompt/history; rejected before opening a provider client |
| Provider output | Existing four compatible-provider caps remain 700 tokens; Gemini now capped at 8,192 tokens |
| Provider HTTP response | 8 MiB decoded and 8 MiB + 64 KiB encoded, checked before SDK expansion; bounded gzip and wrapped/raw deflate |

The 180-second upload ingestion budget accommodates about 20 MiB over a 1 Mbps
connection, with modest headroom. Uploads remotely validate authentication and
acquire per-user admission **before reading/buffering a body**. The route reuses
only that same request's validated user; another request validates again. Invalid
credentials cannot reserve a long-lived upload spool. Actual bytes are checked
even without Content-Length, and inconsistent/duplicate framing is rejected.
Unsupported methods do not receive the large upload/chat body allowance. CORS
preflight and CORS headers on early rejections remain supported.

No workload waits in an application admission queue. Process-local admission
allows at most 24 active API requests and retains at most 4,096 user buckets;
idle rate history expires after ten minutes. Health and actual CORS preflight
requests do not consume API permits. Token buckets start with the listed burst:

| Class | Global concurrent | Per-user concurrent | Global requests/minute (burst) | Per-user requests/minute (burst) |
| --- | --- | --- | --- | --- |
| Reads | 8 | 4 | 300 (60) | 120 (20) |
| Chat | 4 | 1 | 60 (8) | 20 (4) |
| Upload | 2 | 1 | 20 (4) | 10 (2) |
| Other writes | 8 | 2 | 120 (20) | 30 (5) |

Rate/per-user concurrency rejection returns sanitized 429; global capacity or
worker saturation returns 503. Oversized resources return 413; malformed input
or documents return 422; expired deadlines return 504. Pydantic errors retain
location/type/message but omit input/context that could echo a key or document.
All successful response schemas, provider models, prompts, grounding and
Basic/Manual/Auto provider selection/fallback order are preserved.

### Startup deadlines, authentication and retry policy

The two listing GETs now use a shared stateless native async HTTPX pool, while
other database services retain the existing SDK. Both still validate the caller
remotely against Supabase Auth on every request, and apply the same ownership
filters, database projections and ordering. Credentials are request-local;
cookies, cross-origin redirects and HTTPS downgrades remain blocked. Native
HTTPX retains verified TLS and environment proxy behavior.

The startup deadline is 10 seconds from in-app admission, covering ingestion,
Auth, database reads, bounded decoding, serialization and ASGI response sending.
Up to 100 ms of that total is reserved for a generated error response (work
normally has 9.9 seconds). Middleware-generated errors have a 100 ms send budget,
clipped to remaining startup time; they also race disconnect and retain ownership
through cancellation cleanup. Global admission rejections use the same bounded
delivery helper. No extra error-delivery grace is added outside the total budget.
Auth and database phases each receive at most four seconds, clipped to remaining
time; connection/pool timeouts are two/0.5 seconds. There are **no startup upstream
retries or Retry-After sleeps**. Cancellation/disconnect closes in-flight streams.
Invalid/expired credentials remain 401; network errors, service limits/outages,
malformed Auth responses and missing backend configuration return sanitized 503.
The resumed review found and fixed unhashable Auth error codes causing 500.

Startup responses remain buffered until preparation succeeds within the budget.
The resumed review also found and fixed deadline gaps during successful and
middleware-generated-error ASGI sends:
the owned task now includes forwarding, so backpressure and disconnect cannot
leave its admission lease running indefinitely. If headers may already have
been emitted, expiry aborts the partial response; HTTP cannot legally replace
that status with a second 504. Completed error-delivery tasks also have their
exceptions retrieved when completion races cancellation, preventing unhandled
task diagnostics from exposing a transport exception. OS/runtime stalls and synchronous bounded JSON
work cannot be preempted at an exact clock tick; proxy/platform time before ASGI
admission and actual wire delivery after server buffering are outside this budget.

Both provider SDKs now make **one generation POST attempt**, including on timeout,
503 and `Retry-After: 120`. The legacy `PHILOMATH_PROVIDER_MAX_RETRIES` setting is
still accepted for configuration compatibility (default now zero), but does not
enable generation replay. Existing provider inactivity/connect timeouts remain
30/five seconds. No new write retry was added: tests confirm installed PostgREST
INSERT/PATCH/DELETE do not retry timeout/503/520. Sequential application fallback
and persistence failure behavior remain unchanged for separate Batch 3 review.

The frontend is unchanged: each loader can still attempt six times with a
12-second window and 26.5 seconds of total backoff. A sustained four-second Auth
outage therefore has a nominal 50.5-second schedule, while six ten-second backend
deadlines still total 86.5 seconds plus scheduling/network overhead. Platform
cold-start delay outside ASGI can still reach the frontend's nominal 98.5-second
timeout schedule. Batch 2 bounds overlapping upstream work; it does not eliminate
all retries, a 1–2 minute loading experience, or Render Free cold starts.

### Final validation and measured evidence

**220 tests passed, zero failed, zero skipped, in 7.364 seconds**, using the
socket-blocked runner. This preserves prior coverage and adds 112 tests relative
to the 108-test Batch 1 suite; prior expectations for approved Auth/retry/input
behavior were updated. Complete OpenAPI comparison with original `2f13d61` found
only intentional `ChatRequest` changes: `mode` enum and `maxLength` constraints
for message (32,000), context (502,000), conversation ID (200) and provider (80).
All paths, methods, operation IDs, parameters/defaults, successful responses and
other models match. Error runtime behavior intentionally changes as documented
above; OpenAPI equality elsewhere does not imply unchanged error handling.

One full run alongside the CPU-active benchmark produced two timing-fixture
errors: synthetic 20/30 ms deadlines left only 2/3 ms for real spool cleanup
and sending 504. The independent run of that code passed. Those two fixtures now
use 200 ms (and 400 ms simulated serialization), retaining all cancellation,
504, recovery and content-suppression assertions; production budgets are
unchanged. The final complete serial run passed all 220. The reviewer approved
this adjustment; the transient failure is not omitted from the validation record.

Coverage includes missing Content-Length, boundary/excess aggregate sizes,
malformed framing/documents, compressed bombs, CPU/address-space enforcement,
slow streams/disk/handlers, concurrent users, saturation/recovery, repeated
cancellation during spawn/write/reap/send, real multipart spool cleanup,
provider failures and redirect/proxy/cookie security. A separate guarded real
ASGI probe cancelled startup and an actual upload twice: zero upstream reads,
zero extra pending asyncio tasks, zero leases/inflight requests, one of one child
processes killed/reaped, recovered parser slot and closed HTTP pool afterward.
Synchronous generation/database work intentionally retains its permit after
caller cancellation until it finishes; this is bounded tracked work, not claimed
to be forcibly cancellable. Lifespan drains it before closing transports.

Three fresh synthetic-process runs, with five milliseconds artificially added
to each normal Auth/database call, measured:

| Diagnostic | Median |
| --- | --- |
| App import | 222.895 ms |
| Lifespan startup | 0.040 ms |
| First authenticated startup GET | 39.155 ms |
| Warm conversations/integrations GET | 11.920 / 13.370 ms |
| Recovery request after cancelled upstream waits | 1.670 ms |

The Supabase SDK remains unloaded even after the first startup GET. At 1:100
scale, both unchanged frontend loaders made six attempts, each receiving 503
before its 120 ms window. Median elapsed time was 0.527009 seconds, maximum
concurrent upstream operations two, 12 upstream waits cancelled, zero active at
frontend exit, all pools closed. This is a scaled simulation, not measured
production latency or a claim of a 50-second real-world improvement.

A separate 1,000-request ingress microbenchmark rejected a declared upload body
one byte above 21 MiB in median 0.003776 ms, with **zero body reads and zero
downstream calls** (ingress-only measurement; full uploads authenticate first).
Normal fixture DOCX extraction in isolated workers took 81.733–86.536 ms and PDF
100.159–105.436 ms; maximum child RSS was 68,464 KiB. These quantify containment
overhead on small fixtures, not speedups or worst-case memory bounds. Provider
regressions prove one POST and no retry sleeps after ambiguous failures.

Reproduce from `backend`, using the unchanged installed dependency set:

```bash
PYTHON_DOTENV_DISABLED=1 .venv/bin/python -B -m tests.run_offline
.venv/bin/python -B scripts/benchmark_startup_controls.py --runs 3
PIP_DISABLE_PIP_VERSION_CHECK=1 .venv/bin/python -B -m pip check
git diff --check
```

The old `benchmark_transport_stabilization.py` describes the historical SDK GET
path and is not the current startup benchmark. No vulnerability advisory scan,
clean dependency reinstall, live providers, real database or production load
validation was performed.

### Exact Batch 2 file list

Paths are relative to `backend`; files outside this list retain their pre-Batch-2
fingerprints. There are 19 existing files changed and 14 new files, no removals.

| File(s) | Change |
| --- | --- |
| `app/main.py` | Middleware ordering, sanitized validation and shutdown/drain lifecycle |
| `app/models/schemas.py` | Input lengths, exact modes and upload context headroom |
| `app/request_limits.py` (new) | Global/user admission, startup deadline and owned cancellation |
| `app/ingress.py` (new) | Streamed pre-parser limits and bounded spool replay |
| `app/document_extraction.py`, `app/document_worker.py` (new) | Terminable subprocess and document resource budgets |
| `app/routes/files.py` | Async prevalidated auth, extraction, aggregate limits and cleanup |
| `app/routes/conversations.py`, `app/routes/integrations.py` | Only startup listing GETs use cancellable reads |
| `app/services/startup_service.py` (new) | Stateless async Auth/database reads, bounded response decoding |
| `app/services/auth_service.py` | Distinguish invalid credentials/outages; admission after validation |
| `app/providers/budgets.py`, `app/providers/response_limits.py` (new) | Input/output constants and streamed HTTP response bounds |
| `app/providers/clients.py` | Single-attempt SDK generation, response-stream guard |
| `app/providers/cerebras_provider.py`, `app/providers/gemini_provider.py`, `app/providers/grok_provider.py`, `app/providers/nvidia_provider.py`, `app/providers/openrouter_provider.py` | Input budgets; Gemini output/retry bounds; prompts/models unchanged |
| `app/routes/chat.py` | Propagate only new input-budget rejection without fallback/persistence |
| `app/settings.py` | Legacy retry compatibility/default zero |
| `tests/test_api_contracts.py`, `tests/test_files.py`, `tests/test_provider_clients.py`, `tests/test_settings.py` | Adapt prior assertions to approved policies; preserve prior security/contracts |
| `tests/test_extraction_limits.py`, `tests/test_ingress.py`, `tests/test_non_idempotent_requests.py`, `tests/test_provider_budgets.py`, `tests/test_request_limits.py`, `tests/test_startup_deadlines.py` (new) | Focused adversarial, boundary, isolation, cancellation and recovery regressions |
| `scripts/benchmark_startup_controls.py` (new) | Fresh-process timings and scaled frontend retry diagnostics |
| `PERFORMANCE.md` | Current limits, evidence, compatibility, review and release limitations |

### Independent review and remaining release blockers

Fresh read-only reviews covered resource/extraction/admission and
Auth/startup/provider transport. The transport reviewer independently reran
**220 tests in 7.051 seconds** and verified all four resumed-session corrections;
the resource reviewer independently passed **219 tests in 7.675 seconds** before
the final exception-retrieval regression was added, and probed disconnect during
real FastAPI multipart parsing and blocked 504 delivery. No unresolved blocking
Batch 2 finding remains. **Batch 2 verdict: READY FOR STAGING. Overall production
release: NEEDS FIXES.**

Remaining limitations/blockers:

- Batch 3: successful generation can still trigger another provider after a
  persistence/count failure; ambiguous writes and separately resent requests
  lack exactly-once guarantees. Existing chat exception reflection remains
  inherited. No persistence semantics were silently changed.
- Batch 4: prior database/RLS policy findings and actual grants still require
  approved remediation/verification; no schema or policy migration was run.
- Rate/concurrency/parser budgets are per process and multiply with workers or
  replicas. There is no distributed limiter or complete DDoS protection.
- The 384 MiB child limit is address space, not total backend/container RSS.
  PDF nested-resource aggregate work partly relies on CPU/memory limits.
  Process isolation and Python socket blocking are not an OS security sandbox.
- Sync provider/database reads retain inactivity timeouts, not an overall chat
  fallback deadline. Slow-drip upstreams and OS-level hung disk I/O can retain
  bounded permits and delay shutdown. No unsafe thread termination is attempted.
- Character budgets are not model-specific token accounting. Staging must
  verify representative documents, provider context windows, real latency,
  connection/worker pressure and worst-case RSS against the hosting limit.
- Bounded JSON decoding/serialization remains synchronous. Live proxy/TLS,
  upstream error mapping and throughput remain unmeasured; no code change can
  remove Render platform cold starts or the unchanged frontend's retry schedule.

## Approved Batch 1 stabilization

At the Batch 1 checkpoint, this superseded the paused logging/dependency status
in the historical transport notes below. Only Batch 1 was then implemented: equivalent linear routing
matching, bounded fail-open performance logging, focused regression coverage,
and dependency pins matching the tested environment. Deferred startup deadlines,
authentication status handling, persistence/fallback behavior, upload/input
limits, rate limits, and database policy/schema work remain unchanged.

- `app/services/brain/profiler.py`: replace only the math digit/operator/digit
  regex with a single forward scan. ASCII digits and all six operators are
  preserved. Only LF interrupts a candidate, as with the previous regex;
  carriage returns and Unicode line separators still match. Square-root and
  keyword scoring, all other signals, and provider selection are preserved.
- `app/observability.py`: capture timing metadata and request IDs into a queue
  with capacity 256 using `put_nowait`. A single daemon performs JSON encoding,
  filters, and handler I/O. Full queues drop the newest record. Encoding/handler
  failures cannot change application results; timer/middleware cleanup also
  independently protects the original exception and request context.
- `app/main.py`: shut down the logging worker after transport cleanup, even if
  cleanup fails. The default logging join budget is 100 ms; undelivered records
  may be lost. A stuck handler cannot be forcibly killed, but reconfiguration
  retains its worker and does not create additional blocked logging threads.
  Logging resumes after that worker exits and the next configuration call.
  Application cleanup is bounded; Python's global handler shutdown and an
  externally installed handler's own behavior are outside this guarantee.
- `requirements.txt`, `requirements.lock`, `DEPENDENCIES.md`: exact versions for
  the existing 11 direct dependencies and complete 61-package runtime closure
  on CPython 3.12.14/Linux x86_64. No dependency installs/upgrades were performed.
  No vulnerability advisory scan, clean reinstall, index availability check,
  or artifact-hash verification was performed.

New routing tests compare 97,656 exhaustive short strings, 6,000 seeded random
cases and math scores, and 259 complete profiles with the former implementation.
Character-visit assertions verify a single pass for adversarial nonmatches and
early exit for matches without flaky timing thresholds. Logging regressions
cover concurrent request context, bounded queues, slow/failing handlers,
serialization failures, original exceptions, and lifecycle cleanup.

Local synthetic measurements (not production endpoint/hosting speedups):

| Measurement | Previous implementation | Batch 1 |
| --- | --- | --- |
| Math-pattern nonmatch, 16,000 ASCII digits (median of 3) | 733.975 ms | 0.756 ms |
| Math-pattern nonmatch, 1,000,000 ASCII digits (median of 3) | Not run: quadratic legacy cost | 45.083 ms |
| Twenty synthetic requests, 10 ms log sink per record | 206.826 ms | 0.551 ms |
| Concurrent 5 ms event-loop timer during those requests | 206.905 ms | 5.706 ms |

The logging benchmark delivered all 20 records in each run and waited for the
background sink to drain separately. Under saturation or shutdown, logging is
best effort and records can be dropped. Measurements vary with machine load;
the deterministic routing visit counts and event-based logging tests establish
the algorithm and isolation guarantees. Neither change removes network latency,
retry amplification, persistence ambiguity, or Render Free cold starts.

Reproduce from `backend`, without credentials or services:

```bash
.venv/bin/python -B -m tests.run_offline
.venv/bin/python -B scripts/benchmark_routing_linear.py
.venv/bin/python -B scripts/benchmark_logging.py
.venv/bin/python -B -m pip check
```

Final Batch 1 validation: **108 tests passed in 3.026 seconds; none skipped**
(the original 84 plus 24 added tests). The complete OpenAPI document equals the
original HEAD document, using isolated baseline/current subprocesses with sockets
blocked. `pip check` and `git diff --check` pass. Fingerprints identify exactly
13 Batch 1 files below; the other 120 pre-existing repository files, including
all prior transport/provider/Supabase/chat/upload changes, remain byte-identical.
Frontend, schemas/migrations, launch/deployment configuration, and environment
files were not changed. No production services, credentials, commits, pushes,
merges, migrations, or deployments were used.

| Batch 1 file | Purpose |
| --- | --- |
| `app/services/brain/profiler.py` | Equivalent linear math-expression scan |
| `app/observability.py` | Bounded background logging and fail-open context cleanup |
| `app/main.py` | Logging shutdown in final transport cleanup |
| `tests/test_routing_linear.py` | Legacy equivalence and adversarial linear-work tests |
| `tests/test_observability.py` | Concurrent requests, async/threadpool context, blocked/failed sinks and failure paths |
| `tests/test_transport_lifespan.py` | Logging cleanup after database cleanup failure |
| `tests/test_dependency_lock.py` | Direct pins and complete active runtime dependency closure |
| `requirements.txt` | Existing direct dependencies pinned; activate constraints |
| `requirements.lock` | Exact transitive runtime constraints |
| `DEPENDENCIES.md` | Tested target, verification and reproducibility limitations |
| `scripts/benchmark_routing_linear.py` | Bounded synthetic regex/scan comparison |
| `scripts/benchmark_logging.py` | Synthetic slow-sink request/ticker comparison |
| `PERFORMANCE.md` | Scope, measured evidence, validation and remaining limitations |

An independent read-only reviewer reran all **108 tests in 3.129 seconds**, none
skipped, and reproduced the logging responsiveness result. It found no new
Critical, High, or Medium issues or required corrections in this batch.
**Review verdict: READY FOR STAGING for Batch 1 only. Overall application:
NEEDS FIXES** because the deferred audit findings remain. The offline suite does
not validate live providers, real Auth/database behavior, production latency,
malicious document processing, or the deferred persistence/deadline policies.

## Transport stabilization follow-up

The user selected **transport fixes only** for this pass. The proposed total
startup deadlines, changed authentication error responses, and persistence-failure
behavior were not approved. Logging and dependency edits were paused and restored.
The earlier optimization notes below describe the original patch; the results in
this follow-up supersede its test count.

Changes in this pass:

- `app/http_safety.py`: common credential-free redirect rejection and cookie jar
  that discards upstream cookies and explicit cookie writes. Same-origin HTTPS
  redirects remain supported; cross-origin, changed-port, userinfo, malformed,
  and HTTP/downgrade redirects are rejected before follow-up requests. Direct
  requests to explicitly configured HTTP gateways remain possible, but even
  same-origin HTTP redirects are intentionally rejected.
- `app/providers/clients.py`: native HTTPX clients manage environment proxies,
  NO_PROXY, verified TLS, and bounded pools. Shared clients are stateless; outer
  SDK clients retain only their own per-call headers/cookies. All pools get a
  cleanup attempt even if another close fails.
- `app/providers/gemini_provider.py`, `app/main.py`: one application-owned unused
  async wrapper satisfies the SDK's supported async-client option, avoids
  per-call async pools, cannot send requests, and closes during lifespan cleanup.
- `app/services/supabase_service.py`: the shared Auth/PostgREST pool rejects cookie
  state and unsafe redirects. A private constructor seam supports socket-free
  first-request measurements. Authentication validation and timeouts are unchanged.
- `tests/test_http_safety.py`, `tests/test_supabase_transport.py`,
  `tests/test_transport_lifespan.py`, `tests/test_provider_clients.py`: regressions
  cover overlapping users, native proxy dispatch, nested native response streams,
  blocked redirects, allowed 307/308 replay, failure cleanup, and SDK lifecycle.
  Observational retry tests retain the current retry policy, including a possible
  120-second server-directed Retry-After wait; they do not claim that it is fixed.
- `scripts/benchmark_transport_stabilization.py`: reproducible synthetic import,
  first/warm authenticated GET, and scaled frontend retry diagnostics.

Final transport validation: **84 tests passed in 1.302 seconds**, none skipped.
The complete generated OpenAPI document still equals the original baseline;
dependency and whitespace checks pass. No frontend, SQL/schema/migration, root
launch script, environment file, or production setting changes were made.

At that transport-only checkpoint, a second independent review found no remaining blocker in the implemented
transport scope. Its additional real native-wrapper probes were promoted to
permanent tests. **Overall release verdict: NEEDS FIXES**, because the explicitly
deferred deadline, persistence/fallback, authentication-401, blocking logging, and
dependency reproducibility findings remain unresolved.

Three fresh-process runs with real FastAPI/Supabase SDK and MockTransport, each
normal Auth/database call delayed artificially by 5 ms, measured these medians:

| Synthetic measurement | Median |
| --- | --- |
| App import | 204.660 ms |
| Lifespan startup | 0.083 ms |
| First authenticated conversations GET, including lazy SDK import | 134.658 ms |
| Warm conversations GET (15 samples) | 12.129 ms |
| Warm integrations GET (15 samples) | 12.271 ms |

These are diagnostic timings, not before/after speedups or production latency.
They omit real sockets, TLS/proxy handshakes, connection contention, database
execution, and Render provisioning. Lazy SDK work is still moved to first use.

The 1:100 frontend retry simulation uses 120 ms attempt windows and 80 ms each
for synthetic Auth/database waits. Requests take about 163 ms. Both loaders
exhaust six attempts after roughly 0.99 seconds, matching the nominal 98.5-second
unscaled schedule plus scheduling overhead. Twelve backend requests complete,
four overlap, and two remain active when simulated frontend retries finish.
Shielded ASGI tasks intentionally model server work surviving a browser timeout;
this is not a real browser/disconnect or HTTPX timeout-enforcement test. All tasks
drain before the one owned HTTP pool closes; no network calls are attempted.

Reproduce from `backend`:

```bash
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -B -m tests.run_offline
.venv/bin/python -B scripts/benchmark_transport_stabilization.py --runs 3
```

## Original optimization evidence

These changes are local development changes. No frontend files, environment
files, migrations, database schemas, production settings, or deployments changed.
No production services or user data were accessed. All network-dependent tests
use fake data and in-memory transports.

## Changes and preserved behavior

| Files | Change |
| --- | --- |
| `app/main.py`, `app/observability.py` | Log application import/startup, HTTP requests, auth, database service operations, file extraction, and provider attempts. Request correlation uses generated IDs; HTTP logs contain route templates, never raw paths/query strings. Lifespan closes owned transport pools after requests drain. |
| `app/settings.py` | Load supported dotenv configuration once without editing it; validate non-secret timeout/retry settings. Never store credentials in settings or logs. |
| `app/services/supabase_service.py` | Initialize the SDK/client lazily under a lock, share one HTTPX pool between auth/database, use separate timeouts, preserve verified TLS and environment proxies, close the pool on shutdown. Configuration presence remains distinct from connectivity. |
| `app/services/auth_service.py` | Add timing only. Every bearer token still goes through Supabase `auth.get_user()`. Existing errors and validation are preserved. |
| `app/services/conversation_service.py` | Use HEAD for exact message counts; request minimal responses for ignored writes; share a successful recent-history snapshot within one chat request. Ownership checks and user filters remain. |
| `app/services/integration_service.py` | Add timing; request minimal responses for integration deletion. Encryption and metadata responses are unchanged. |
| `app/routes/chat.py` | Scope history reuse to a single `send_chat()` call, resetting it even on failure. No routing/fallback logic changes. |
| `app/routes/files.py` | Defer PDF/DOCX imports until needed, parse documents in the worker pool, read at most the existing 10 MB limit plus one byte before rejecting oversized files, and close unsupported uploads. |
| `app/providers/clients.py` | Share connection transports while using separate SDK/HTTP clients per call. No global API-key cache or shared per-user headers/cookies. Explicit timeout/retry settings and shutdown cleanup. |
| `app/providers/cerebras_provider.py`, `gemini_provider.py`, `grok_provider.py`, `nvidia_provider.py`, `openrouter_provider.py` | Lazy SDK imports, common client lifecycle, timing. Preserve all five providers, prompts, models, grounding, call signatures, and optional developer-key fallback. |
| `requirements.txt` | Declare the existing transitive HTTPX dependency explicitly; no new package installation was needed for this patch. |
| `tests/__init__.py`, `tests/run_offline.py`, `tests/test_api_contracts.py`, `tests/test_database_services.py`, `tests/test_files.py`, `tests/test_observability.py`, `tests/test_provider_clients.py`, `tests/test_routing_contracts.py`, `tests/test_settings.py`, `tests/test_supabase_service.py` | Offline regression tests and network-blocked runner. |
| `scripts/benchmark_import.py`, `PERFORMANCE.md` | Reproducible import comparison against a Git revision and this review guide. |

Basic, Manual, and Auto candidate selection, ordering, weak-answer handling,
model selection, and fallback behavior remain intact. No providers run in parallel.
The generated OpenAPI document was compared with the original commit and is
identical. Authentication validation, service-role ownership checks, and encrypted
BYOK storage were not replaced or bypassed.

History caching is lazy, request-local, and keyed by user, conversation, and limit.
It caches empty successful results, never failures, and invalidates on message
writes/deletion. A concurrent chat's writes do not change the snapshot midway
through provider fallbacks; subsequent requests fetch fresh history. Credentials
and histories are never cached across chat requests.

## Default budgets

| Setting | Default | Scope |
| --- | --- | --- |
| `PHILOMATH_CONNECT_TIMEOUT_SECONDS` | 5 seconds | Supabase connection/pool acquisition |
| `PHILOMATH_AUTH_TIMEOUT_SECONDS` | 10 seconds | Supabase Auth read/write inactivity |
| `PHILOMATH_DATABASE_TIMEOUT_SECONDS` | 15 seconds | Database read/write inactivity (SDK previously defaulted to 120 seconds) |
| `PHILOMATH_PROVIDER_CONNECT_TIMEOUT_SECONDS` | 5 seconds | Provider connection/pool acquisition |
| `PHILOMATH_PROVIDER_TIMEOUT_SECONDS` | 30 seconds | Provider read/write inactivity (OpenAI SDK previously defaulted to 600 seconds) |
| `PHILOMATH_PROVIDER_MAX_RETRIES` | 1 retry | One initial attempt plus one retry; configurable from 0 to 2 |
| `PHILOMATH_PERFORMANCE_LOGGING` | `true` | Set to `false` or `0` to disable performance logs |

These are per-phase/per-attempt timeouts, **not a hard deadline for the entire
endpoint or fallback chain**. The installed PostgREST SDK retains its existing
GET/HEAD retries for 503/520, including backoff. Existing provider fallback chains
remain sequential; their attempts accumulate. A slow provider can time out sooner
and trigger the same existing fallback/error path. Choose settings using measured
workload requirements before any eventual deployment. No production values were
changed here.

## Evidence

On this development machine, Python 3.12, five fresh subprocesses per revision,
same installed dependencies, dotenv disabled, and sockets blocked:

| Measurement | Original commit `2f13d61` | Working tree |
| --- | --- | --- |
| Median application import | 0.949 seconds | 0.258 seconds |
| Sample range | 0.909–0.982 seconds | 0.247–0.264 seconds |

This is approximately 73% less **local import time**. It measures neither Render
container provisioning nor a fully authenticated first request. Lazy imports
move some SDK work to first use; they do not eliminate it.

Offline tests also confirm:

- Five history loads in one chat request produce one database read, compared
  with five without request-local reuse; user/conversation/request isolation holds.
- Exact counts send HEAD and parse the count header without fetching ID rows.
- Ignored message-write results use `return=minimal`; writes still check ownership.
- Shared Supabase initialization issues no network probes and occurs once under
  concurrent first access. Auth JWTs do not replace the database service header.
- Provider calls reuse transport pools without retaining another user's key or
  cookie; all five request formats, retry counts, and timeouts are checked using
  the installed SDKs over offline transports.
- Other event-loop work progresses while a deliberately blocked document
  extractor runs in a worker thread.
- All original endpoint methods, auth rejection behavior, response contracts,
  BYOK encryption, and Basic/Manual/Auto fallback order have regression coverage.

Final validation: **63 tests passed in 0.820 seconds**, no skipped tests;
`git diff --check` and installed dependency checks passed. Git comparison confirms
the frontend, migrations, and root launch script are unchanged.

Production network latency, database execution plans, and the reported 1–2 minute
initialization delay remain unmeasured. The local baseline did not reproduce that
delay. Render Free cold starts cannot be eliminated by these changes.

## Run validation

From `backend`, using the existing virtual environment:

```bash
.venv/bin/python -B -m tests.run_offline
.venv/bin/python -B scripts/benchmark_import.py --baseline-ref 2f13d61 --runs 5
```

The runner disables dotenv, removes service/provider credential bindings in the
test process, and rejects network sockets. SDK-backed tests use `MockTransport`
or equivalent fake transports. No credentials or database services are required.
The benchmark extracts only the baseline backend application into a temporary
directory, uses the current installed dependencies, and blocks network access.

Tested SDK versions: FastAPI 0.143.2, Starlette 1.8.0, Supabase/PostgREST/Auth
2.33.0, OpenAI 3.31.0, google-genai 2.31.0, HTTPX 0.28.1, cryptography 50.0.2.
At that original checkpoint, requirements remained unpinned; Batch 1 above now
pins the tested environment. Re-run these checks
in any future target environment; older SDKs may not support the transport options.

## Investigate the hosted delay after review

Use the new logs to separate platform startup from app import, first client
initialization, Supabase Auth, database operations, and provider calls. Compare a
first request after inactivity with warm requests. Log fields contain timings,
fixed operation labels, route templates, status/outcome, and generated correlation
IDs; no bodies, tokens, user IDs, exception text, or credential URLs are emitted.

If initial conversations and integrations are both slow, inspect shared Auth/DB
timings first: neither listing endpoint invokes AI providers. The root health
endpoint remains a lightweight liveness check and does not establish database
readiness. If time is spent before app logs appear, investigate Render lifecycle
events. No production investigation was performed as part of this patch.

Pagination, schema/index changes, JWT validation changes, changed API error
responses, and an overall fallback-chain deadline are deliberately outside this
patch because they require separate review. Existing synchronous endpoints run
in FastAPI's worker pool; sustained slow upstream traffic can still exhaust those
workers. A larger async redesign should follow measurements, not precede them.
