# Release review and rollback checklist

No release is authorized by this document. Batch 5 code is **READY FOR STAGING**;
production release is **NEEDS FIXES / VALIDATION PENDING**. See
[BATCH5.md](BATCH5.md) for measured evidence, exact changes and coverage limits.
The current work is uncommitted. Do not merge, push, migrate or deploy until the
user approves the concrete release and its environment-specific plan.

## 1. Preserve and review the release candidate

- Record the source revision, uncommitted diff, dependency lock versions and
  separately approved database artifacts. Include only intended source/tests/docs;
  exclude `.env`, credentials, private records, certificates, dumps and build logs.
- Rerun the socket-blocked regression suite, frontend logic tests, semantic
  TypeScript check and complete OpenAPI comparison if source changes further.
  Current results: 363 Python tests, 25 Node tests, synthetic browser workflows,
  separate TypeScript check and exact OpenAPI comparison passed.
- Keep deployment-branch review explicit. The previously reported settings are
  Render production/main with on-commit auto-deploy and Vercel production/main
  with previews for unassigned branches. Verify current dashboard settings before
  any future push; a development branch can still create a preview. Do not use a
  production-linked preview as an isolated staging test.

## 2. Check environment bindings without exposing values

- Backend: verify `SUPABASE_URL` and `SUPABASE_SERVICE_ROLE_KEY` identify the intended
  project; keep the privileged key server-only. The existing `SUPABASE_KEY` fallback
  is not a reason to use a browser key for backend writes. Verify permissions
  through an authorized test, not by printing credentials.
- Preserve the existing valid `PHILOMATH_ENCRYPTION_KEY` when moving the same
  encrypted integrations. Losing or casually rotating it makes saved BYOK keys
  unreadable. Any rotation requires a separate reviewed re-encryption/recovery
  plan. Never reuse production encryption/provider credentials for synthetic tests.
- Frontend: `NEXT_PUBLIC_SUPABASE_URL`, `NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY` (or
  existing anon-key fallback), and `NEXT_PUBLIC_BACKEND_URL` must reference the
  intended environment. These values are baked into a Next build. Never expose
  service-role or encryption keys in a `NEXT_PUBLIC_*` variable.
- Check `FRONTEND_ORIGINS` against exact approved HTTPS application origins and
  the OAuth redirect allowlist against that environment. No wildcard convenience
  changes, disabled TLS, provider credential redirects or public SQL endpoints.
- Preserve reviewed timeout/resource defaults. `PHILOMATH_PROVIDER_MAX_RETRIES`
  remains a compatibility setting, not authorization to retry generation POSTs.
- Confirm the previously exposed **staging database password has been rotated**
  and saved privately, and assess existing connections according to the provider's
  revocation guidance. The user's hosted-test pass does not establish rotation.
  Do not paste credentials into chat, command arguments, Git or diagnostic logs.

## 3. Review database promotion separately

Batch 4 hosted Auth/RLS/ownership/upsert/cleanup success was reported by the user.
This batch did not connect to that project or apply SQL. The authorized staging
reference remains `tdiqzmaqvoynspmilwra`; production `inlzipnacliyhdahwcak` was not
accessed. Existing guarded tools retain verified TLS and the optional trusted CA.
Do not rerun baseline creation on an existing database.

- Review retained hosted reports against the exact candidate source/artifact
  hashes. Do not treat mocked workflows or local Auth stubs as hosted evidence.
- Before any separately authorized production plan, obtain schema-only evidence
  for remaining production security context: PUBLIC/column/inherited/default
  privileges, memberships, schema ACLs, FORCE RLS, valid indexes, function ownership
  and EXECUTE, views/RPCs/event triggers, Auth hooks and relevant service versions.
  Known table metadata and staging defaults do not prove production equivalence.
- Keep `supabase/drafts/002_backend_only_writes.sql.draft` and
  `003_parent_owner_integrity.sql.draft` outside automatic migration discovery
  until their promotion is reviewed. They are not new Batch 5 changes. Review
  exact guards, grants, trigger bodies, transaction boundaries and postconditions;
  never copy unguarded snippets into an arbitrary SQL editor.
- Plan bounded lock/index work for the actual table sizes. Preserve original
  foreign keys and NULL-owned legacy records. The new composite FK is deliberately
  NOT VALID; it does not certify or repair historical ownership inconsistencies.
- Review changed legacy deletion outcomes explicitly: guarded NULL/inconsistent
  children may prevent parent/Auth-user deletion atomically. Do not guess owners,
  delete legacy rows or bypass guards to make a rollout pass.
- An acknowledged transaction failure rolls back. A lost commit reply is
  ambiguous: inspect the catalog/state before any retry. Never blindly retry
  hardening, data writes or Auth-user creation after an uncertain outcome.

No database execution is part of this checklist. Any production migration needs
separate approval, a reviewed backup/recovery plan, exact target verification and
an operator-controlled window.

## 4. Close live-service and latency gaps in isolated staging

Use only synthetic records/test accounts and explicitly authorized staging keys.
Keep billing limits and permitted test traffic agreed in advance.

1. Run an ordinary frontend production build with actual font delivery, then real
   Google test-account sign-in/out, refresh/expiry, rejected OAuth callback and
   account switching. Synthetic OAuth passing does not validate Google settings.
2. Exercise Basic/Manual/Auto, all five actual provider models, BYOK save/replace/
   delete, uploads, persisted history and failed-save recovery. Check the configured
   Grok model exists for the test account. Do not automatically replay a failed
   generation or uncertain save; first reload history.
3. Measure cold platform arrival separately from ASGI admission, SDK first use,
   warm requests and upstream duration. Capture distributions over repeated runs,
   not just averages. Load within staging safety/cost limits, including concurrent
   users and document-worker RSS. Existing local values are not hosting targets.
4. Verify 401 for invalid tokens, 503 for Auth outages, 504 for startup deadlines,
   413/422 for bounded input failures, and 429/503 saturation/recovery. Confirm
   client disconnects leave no startup reads or upload workers running. Synchronous
   chat work can continue under its retained admission permit until completion.
5. Resolve the total generation-latency policy before promising bounded chat
   completion. Providers currently have 30-second inactivity, not total, deadlines;
   classifier/fallback/database phases accumulate. Chat has no frontend total abort.
   A new shared deadline/cancellation design must preserve non-replay of uncertain
   POSTs and requires explicit approval if it changes failure/fallback behavior.
6. Observe startup retry behavior on the actual host. Backend listing work has a
   ten-second in-app deadline, but platform cold starts occur earlier. Six frontend
   attempts with 12-second windows and 26.5 seconds backoff can still approach
   98.5 seconds. Code does not remove Render Free cold starts. Changing retries,
   lazy loading or hosting requires a separately reviewed decision.

Do not equate these operational validation gaps with new confirmed vulnerabilities.
There is no evidence yet of production latency, real provider availability/cost,
or a tested production rollback.

## 5. Monitor without collecting sensitive data

Use existing bounded, fail-open performance events and hosting metrics first.
No paid service or hosting upgrade is required by this plan.

- Track request and operation p50/p95/p99, first-use latency, Auth/DB/provider
  failures by sanitized category, cancellations, and 413/422/429/503/504 rates.
  Distinguish platform arrival from in-app timings and first SDK use from warm I/O.
- Watch admission saturation, document-worker exits, process/child RSS, CPU,
  connection counts and cleanup recovery. Existing counters may need an approved
  sanitized export before they can be monitored remotely; no new metrics endpoint
  was introduced here. Limits are process-local, not a cluster-wide quota.
- Observe bounded log queue drops/handler failures through safe operational
  diagnostics. Do not make logging a request dependency or enable SDK wire debug
  logs. Never log tokens, keys, cookies, prompts, file bodies, encrypted BYOK values,
  full connection URLs or raw provider/DB exceptions.
- Treat generation success followed by persistence 503 as a distinct reliability
  event. Separate HTTP writes permit partial/ambiguous commits and duplicate
  independent submissions; there is no exactly-once guarantee or durable answer
  recovery store. Existing history may recover a committed answer, not an unsaved
  generated answer.

Set numeric latency/error/RSS thresholds from agreed staging measurements before
release. Avoid invented SLOs based on a five-millisecond mock provider.

## 6. Rollback and release gates

- Before a future approved deployment, record a known compatible prior app
  artifact, environment binding versions and database migration state. App rollback
  must remain compatible with backend-only writes, owner guards and BYOK encryption.
- If a release degrades authentication, isolation, persistence or latency, stop
  new rollout traffic and have the operator select the reviewed compatible app
  artifact. Do not automatically weaken RLS, regrant browser writes, drop ownership
  guards, change encryption keys or restore data as an application rollback.
- A committed database change needs its own reviewed forward repair or recovery
  procedure. There is no generic safe automatic down migration. Preserve ambiguous
  write evidence and legacy data; investigate before replaying requests.
- Final production gate: approved candidate/diff, retained staging reports, live
  OAuth/all-provider workflows, latency/resource acceptance, reviewed production
  schema context and migration effects, monitoring and operator rollback approval.
  **These gates are not all complete. Do not deploy from this report.**
