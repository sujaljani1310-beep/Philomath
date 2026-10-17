# Batch 5 — measured validation and release review

Code review verdict: **READY FOR STAGING**. Production release verdict:
**NEEDS FIXES / VALIDATION PENDING**. The reviewed fixes have no remaining
confirmed blocker; live OAuth/provider checks, production rollout review, and an
agreed total generation-latency policy remain outstanding. Nothing was committed,
pushed, merged, migrated or deployed during this batch.

Work began on `philomath-batch4-ca-fix` at
`2585eacab2a5c7b696b5b89a98be013e3c2b9ad8`. Existing Batches 1–4 are preserved.
The user reported successful hosted Batch 4 Auth/RLS, ownership, browser-write,
concurrent-upsert and cleanup verification. That report is distinct from this
batch's independently executed local tests: no hosted database was contacted.
Earlier Batch 4 documents retain their historical, then-pending validation status.

## Confirmed defects corrected

- An empty or unsaved conversation repeatedly fetched history. Conversations now
  track history loading/completion/failure; local new chats need no history GET.
  Concurrent loads are deduplicated, and selecting a failed history explicitly
  retries it. Sending waits for history; reloading cannot overwrite an active
  generated answer, including across sidebar switches. Same-account token refresh
  preserves loaded history and pending turns; a snapshot protects pending new
  chats even when React defers state updates. A rejected initial session lookup
  clears the loading state.
- Late initial-session, history, integration, chat, upload and mutation callbacks
  could overwrite state after an account change. Identity/generation checks now
  discard stale callbacks and clear account-scoped state. These are the explicitly
  approved changes to `frontend/app/page.tsx`; layout, styling, components and all
  other frontend files are unchanged. Existing startup retry timings are retained.
- Auto Mode discarded its satisfiable search requirement when an unsupported
  inferred file requirement emptied the candidate set. The explicitly approved
  correction retains available search-capable models in that particular case.
  Confirmed unsupported attachments still block; an unavailable search provider
  still permits the existing best-effort path. No scoring weights changed.
- With `GROK_MODEL` configured, the actual SDK request and reported/persisted model
  could disagree. They now resolve the same existing configuration without
  mutating shared registry entries or changing the requested model.
- An unexpected chat exception could expose its text in the answer. It now uses
  a fixed sanitized message, retaining the existing response fields and status.
  Persistence failures still raise sanitized 503 without generating again.

## Measurements and their limits

Environment: Linux Codex Cloud, Python 3.12.14, Node 24.19.0, Chromium 154, the
already-installed locked Python dependencies and frontend node_modules. No
dependencies were installed or upgraded. These are local samples, not a capacity
test, production SLO, vulnerability scan or measurement of Render's infrastructure.

### Original source versus hardened source

Five fresh processes per source revision, serial invocation, socket/DNS blocked,
same installed dependencies, dotenv disabled:

| Application import | Median | Five samples, seconds |
| --- | ---: | --- |
| Original `2f13d61ae16b3f885461d518517434c19775b5ce` | 1.684 s | 1.719, 1.684, 1.631, 1.365, 2.169 |
| Current working tree | 0.404 s | 0.404, 0.398, 0.413, 0.369, 0.643 |

This compares cumulative hardening, not Batch 5 alone. Imports deferred by earlier
batches move SDK costs to first use; they do not erase them. Other invocations
showed different absolute timings. There is no pre-hardening network/database
latency baseline, and no claim of a production speedup.

### SDK, API and memory diagnostics

Three fresh credential-free workers used real installed SDKs against mocked
HTTPX Auth/database/provider peers. Provider response delay was deliberately
5 ms; database requests had no simulated network latency. Ten warm samples per
worker give 30 samples per warm operation. The synthetic admission clock advances
between samples; production rate/concurrency policies are not changed.

| Diagnostic | Result |
| --- | ---: |
| App import, median of three workers | 396.203 ms |
| First Supabase SDK initialization, median | 187.037 ms |
| First OpenAI-SDK chat, median | 513.529 ms |
| First Gemini-SDK chat, median | 399.060 ms |
| Warm conversation list, median / p95 / max | 3.737 / 11.801 / 12.801 ms |
| Warm integration list, median / p95 / max | 3.410 / 7.159 / 7.425 ms |
| Warm conversation history, median / p95 / max | 5.778 / 9.253 / 9.665 ms |
| Warm Manual chat, median / p95 / max | 21.218 / 27.937 / 27.974 ms |
| Approximate process RSS: import / Supabase / first chat / Gemini | 42.6 / 49.8 / 68.6 / 88.9 MiB |

RSS excludes document subprocesses and concurrent/worst-case load. Provider and
database latency are synthetic: these numbers cannot predict hosted response
times or model throughput. First-use measurements include SDK loading overhead,
not just upstream work. All three workers ended with zero active admission
leases and closed database/async pools.

A first persisted chat made **16 Auth/database requests plus one generation**:
one Auth validation; nine conversation GETs; one conversation INSERT; one
integration GET; one history GET; two message INSERTs; one message HEAD count.
The ownership checks and separate acknowledgments explain much of this cost.
Collapsing them safely needs a separately reviewed transactional database API;
this batch does not remove safeguards to reduce round trips.

### Frontend and startup retries

A Chromium test built tracked frontend source in temporary directories with
Next's production webpack build. Real Supabase JS performed a synthetic Google
OAuth handoff/callback; Auth, database and all AI responses were synthetic. Browser
traffic was restricted to loopback fixture origins. The normal Google Fonts build
failed with DNS `EAI_AGAIN`; the successful run used Next's offline font fixture
and local DejaVu Sans. This is not real font delivery or visual regression evidence.

| Browser diagnostic | Result |
| --- | ---: |
| Old `2585eac` unsaved-chat history GETs in 600 ms | 22 |
| Fixed unsaved-chat history GETs in 600 ms | 0 |
| Current production-mode fixture build | 14.555 s |
| Signed-out ready, one sample | 634.941 ms |
| Authenticated reload ready, one sample | 281.552 ms |
| Browser JS heap used / JS transfer on reload | 14.119 MiB / 243,643 bytes |
| Page errors | 0 |

Request elimination is a confirmed improvement. The single browser timing samples
do not establish a comparative loading-time improvement. BYOK, Basic, Manual with
TXT upload, Gemini switching, Auto, injected 503/manual recovery, persisted history
reload and sign-out passed. Five chat POSTs included one intentionally failed POST;
the browser did not automatically replay generation. Final cleanup confirmed the
synthetic server closed, zero leases, and closed database/async pools.

The existing startup simulation ran three times with 5 ms per normal upstream
call. Medians: app import 324.050 ms; lifespan 0.043 ms; first authenticated listing
42.297 ms; recovery 2.033 ms. Warm listings were approximately 11.9–19.4 ms.
At 1:100 time scale, both frontend loaders made six attempts, each receiving 503
within its 120 ms window. Combined elapsed time was 0.536–0.546 seconds; maximum
active upstream reads was two, 12 waits were cancelled, and **zero remained at
frontend exit**. Pools closed afterward. This is cancellation/retry evidence,
not an accelerated production performance claim.

### Why a one-to-two-minute wait remains possible

Startup listings have a 10-second total in-app deadline, including admission,
Auth/database I/O and response work; each upstream phase is clipped to at most
four seconds and has no retry. The frontend still permits six 12-second attempts
plus 26.5 seconds of backoff: a nominal **98.5 seconds** if every attempt times out.
A sustained four-second Auth outage nominally totals 50.5 seconds; six ten-second
in-app expiries total 86.5 seconds before network/scheduling overhead. Platform
queuing/cold start precedes ASGI admission and is outside the backend deadline.
Render Free cold starts are not eliminated by these changes.

Provider POSTs have one SDK attempt, a five-second connect budget and a 30-second
inactivity timeout. **Thirty seconds is not a total chat deadline**: a slow trickle
can continue longer; sequential fallback and the optional classifier add work.
Five providers plus a classifier can nominally consume 180 seconds of individual
30-second waits, before database work, and that is not a strict ceiling. Ordinary
database SDK operations have individual 15-second budgets, not one shared chat
deadline. The frontend chat fetch has no total abort timeout. A product-approved
total generation deadline and cancellation design remains a release consideration;
this batch does not silently change fallback or replay an uncertain POST.

## Auto Mode audit

Auto Mode is task-aware, using deterministic request profiling and capability
filtering, then the current user's available providers. Final scores combine
**55% task capability, 15% speed, 10% inverse cost, 20% reliability**. These registry
ratings are manually assigned heuristics, not measured model quality, current
prices, account limits or live latency. Confidence is a scoring heuristic, not
an empirically calibrated probability.

With all five providers available and the optional classifier returning no
override, representative first choices are Cerebras for general/creative,
Grok for coding/math, Gemini for research/summarization. Research search filters
to Gemini. The combined inferred-file/search regression now does so too. General
ranking is Cerebras (0.7570), Grok (0.7550), NVIDIA (0.7075), Gemini (0.6900),
OpenRouter (0.4775). Rankings depend on prompt profile and user keys.

Low separation below 0.12 can make an extra sequential Cerebras classifier call,
only with that user's Cerebras key. Five of seven diagnostic prompts requested
this path after the narrow fix. The benchmark mocked the classifier result;
its sub-millisecond routing timings exclude classifier network time. The actual
classifier uses the normal provider path and up to 700 output tokens. Failures
leave deterministic ranking intact.

Basic keeps its existing fixed fallback ordering: OpenRouter, Gemini, Cerebras,
NVIDIA, Grok for ordinary requests; Gemini, OpenRouter, Cerebras, NVIDIA, Grok
when its existing search detector matches. Unconfigured providers are skipped.
Manual calls only its selected
provider, and Auto follows its ranked eligible chain and existing answer-quality
rules. Earlier persistence separation still prevents a save/count failure after
an accepted answer from invoking another provider. There is no live health-based
reranking, circuit breaker or learning from failures. A `GROK_MODEL` override now
has truthful metadata but still uses static Grok capability ratings. Those are
limitations to evaluate with synthetic tasks and approved staging keys, not
evidence that weights should be redesigned now.

## Tests, compatibility and independent review

- **363 Python regressions passed, zero failed/skipped, 12.916 s** in an independent
  socket-blocked run: all 345 earlier tests plus eight defect regressions and ten
  real-SDK/synthetic-peer workflow tests. All five provider adapters parsed mocked
  successful responses. Tests also cover ownership/isolation, encrypted BYOK,
  Auth outage/recovery, fallback, lost save acknowledgments, document processing,
  concurrency saturation and recovery. Earlier transport/resource tests remain.
- **25 Node frontend callback tests passed**, zero failed/skipped. They extract
  actual TypeScript callbacks and exercise empty/failed history, concurrent loads,
  loading/session errors, stale account/action races, token-refresh races and
  deferred state updates. This is not a complete
  React component-render suite.
- Chromium workflows above passed. A separate TypeScript `tsc --noEmit
  --incremental false` check passed on the final temporary production build.
  The inherited Next config skips build-time type errors; the separate check is
  therefore material. No configuration was changed to hide failures.
- Complete canonical OpenAPI JSON is identical to `2585eac`: **seven paths, five
  schemas**, including errors documented in the baseline. Equality does not imply
  unchanged error text; sanitization and the approved routing case are intentional.
- `pip check`: no broken requirements. Dependency locks unchanged; no advisory
  scan, clean reinstall, dependency upgrade, real Google/provider API call, hosted
  SQL or new migration execution occurred. Prior Batch 4 local PostgreSQL and
  user-reported hosted results were not rerun as part of this batch.
- One earlier concurrent full run exposed an existing ingress timing-fixture
  failure (one of 363); its tiny scheduler budget was inappropriate under browser
  build contention. The test now observes the actual timeout budgets directly,
  with scheduling headroom. Adjacent real slow-body deadline tests are unchanged.
  The final independent full run passed; production limits were not relaxed.
- A fresh independent read-only reviewer examined all source and tooling, reran
  all 363 Python and 25 Node tests, and returned **READY FOR STAGING** with no
  remaining confirmed blocker in the changes. Live-service limitations, static
  scores, total generation time and non-atomic persistence remain explicit.
  The final review also exposed cross-chat history and same-account refresh
  races, including a deferred-updater variant. Five additional frontend tests
  reproduced those cases before their fixes and passed afterward.

Persistence still consists of multiple requests, not a database transaction.
A 503 can follow an actual commit; explicit resubmission can duplicate work.
Reload history before replaying a failed save. There is no exactly-once guarantee,
new schema, idempotency key or automatic ambiguous-write retry.

## Exact Batch 5 file inventory

All paths below are relative to the repository. Six existing code/test files,
seven new test/tool files, one existing document and two new documents: **16 files**.

| File | Purpose |
| --- | --- |
| `backend/app/providers/grok_provider.py` | Resolve the existing configured model once per call |
| `backend/app/routes/chat.py` | Matching Grok metadata; sanitized unexpected errors |
| `backend/app/services/brain/model_registry.py` | Request-time Grok metadata without registry mutation |
| `backend/app/services/brain/router_brain.py` | Approved narrow inferred file/search correction |
| `frontend/app/page.tsx` | Approved loading/session/history race fixes only |
| `backend/tests/test_ingress.py` | Stable direct assertion of distinct ingestion budgets |
| `backend/tests/test_batch5_regressions.py` | Eight focused defect regressions |
| `backend/tests/workflow_fixture.py` | Isolated real-SDK/mock-upstream workflow fixture |
| `backend/tests/test_workflows.py` | Ten full backend workflow regressions |
| `backend/scripts/test_frontend_loading.cjs` | Twenty-five actual-callback Node regressions |
| `backend/scripts/workflow_server.py` | Network-confined synthetic browser backend bridge |
| `backend/scripts/check_browser_workflows.py` | Temporary frontend builds and Chromium workflows |
| `backend/scripts/benchmark_batch5.py` | Reproducible SDK, API, RSS and routing diagnostics |
| `backend/PERFORMANCE.md` | Link current evidence without rewriting old checkpoints |
| `backend/BATCH5.md` | This evidence and limitation record |
| `backend/RELEASE.md` | Review gates, monitoring, migration and rollback checklist |

No dependency, database schema, migration, environment or deployment file changed.
No credentials, private records, certificates, dumps or generated build artifacts
are part of the change inventory. Measurements/builds/logs stayed in `/tmp`.

## Reproduction

Use already-installed dependencies. From `backend/`:

```sh
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -B -m tests.run_offline
.venv/bin/python -B scripts/benchmark_import.py --baseline-ref 2f13d61 --runs 5
.venv/bin/python -B scripts/benchmark_startup_controls.py --runs 3
.venv/bin/python -B scripts/benchmark_batch5.py --runs 3 --samples 10
.venv/bin/python -B -m pip check
```

From the repository root, with existing Playwright/Chromium and frontend modules:

```sh
node --test backend/scripts/test_frontend_loading.cjs
python -B backend/scripts/check_browser_workflows.py --offline-fonts
python -B backend/scripts/check_browser_workflows.py --offline-fonts --baseline-ref 2585eac --expect-legacy
git diff --check
```

`--offline-fonts` requires the script's Linux font fixture; omit it in a normal
network-enabled build to test real font retrieval. It is not a deployment option.
The browser script reports its temporary build directory; run the installed
`node node_modules/typescript/bin/tsc --noEmit --incremental false` there for the
separate semantic check. All tests use synthetic data. See [RELEASE.md](RELEASE.md)
for the remaining checks requiring later authorization.
