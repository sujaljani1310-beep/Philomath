"""Offline ASGI timings and a scaled reproduction of the existing retry window.

Run with the backend virtualenv, for example::

    .venv/bin/python -B scripts/benchmark_transport_stabilization.py --runs 3

The upstream is HTTPX MockTransport, not a database or provider. Artificial
delays, tiny synthetic rows and in-process HTTP exclude hosting startup, DNS,
TLS, socket pools and real database execution. These are reproducible diagnostic
measurements, not production performance or timeout-enforcement evidence.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import statistics
import subprocess
import sys


BACKEND = Path(__file__).resolve().parents[1]
PATHS = ("/api/conversations", "/api/integrations")
ATTEMPT_SECONDS = 12.0
BACKOFF_SECONDS = (1.5, 3.0, 6.0, 8.0, 8.0)


def _summary(samples: list[float]) -> dict:
    return {
        "samples_seconds": [round(value, 6) for value in samples],
        "median_seconds": round(statistics.median(samples), 6),
    }


def _worker(args) -> dict:
    # Install the guard before any application/SDK import. No credential or
    # dotenv file is read; the controller gives this process a minimal env.
    import asyncio
    from collections import Counter
    import socket
    from threading import Lock
    import time
    from unittest.mock import patch

    blocked_calls = 0

    def no_network(*_args, **_kwargs):
        nonlocal blocked_calls
        blocked_calls += 1
        raise AssertionError("Network access is disabled for this benchmark")

    socket.socket.connect = no_network
    socket.socket.connect_ex = no_network
    socket.create_connection = no_network
    socket.getaddrinfo = no_network

    imported_at = time.perf_counter()
    from app.main import app
    import_seconds = time.perf_counter() - imported_at
    sdk_loaded_after_import = "supabase" in sys.modules

    import httpx
    from app.services import supabase_service

    counts = Counter()
    upstream_lock = Lock()
    upstream_delay_seconds = args.upstream_delay_ms / 1000.0
    real_http_client = httpx.Client
    owned_clients = []

    def upstream(request):
        nonlocal counts
        if request.url.host != "synthetic.example.invalid":
            raise AssertionError("Unexpected upstream origin")
        if request.method != "GET":
            raise AssertionError("Benchmark must be read-only")
        with upstream_lock:
            counts[request.url.path] += 1
            delay = upstream_delay_seconds
        time.sleep(delay)  # Controlled upstream latency in a worker thread.
        if request.url.path == "/auth/v1/user":
            if request.headers.get("authorization") != "Bearer synthetic-user-token":
                raise AssertionError("Unexpected synthetic authentication")
            return httpx.Response(200, json={
                "id": "synthetic-owner", "aud": "authenticated",
                "app_metadata": {}, "user_metadata": {},
                "created_at": "2026-01-01T00:00:00Z",
            })
        if request.url.params.get("user_id") != "eq.synthetic-owner":
            raise AssertionError("Missing user ownership filter")
        if request.url.path == "/rest/v1/conversations":
            return httpx.Response(200, json=[{
                "id": "synthetic-chat", "title": "Synthetic conversation",
                "mode": "basic", "user_id": "synthetic-owner",
                "created_at": "2026-01-01T00:00:00Z",
                "updated_at": "2026-01-01T00:00:00Z",
            }])
        if request.url.path == "/rest/v1/user_ai_integrations":
            return httpx.Response(200, json=[{
                "id": "synthetic-integration", "provider": "gemini",
                "display_name": "Gemini", "api_key_last4": "0000",
                "created_at": "2026-01-01T00:00:00Z",
                "updated_at": "2026-01-01T00:00:00Z",
            }])
        raise AssertionError("Unexpected synthetic upstream path")

    def new_client(**kwargs):
        # Keep the application's hooks/cookies/timeouts, replacing only actual
        # network I/O. Importing the SDK remains part of the first GET timing.
        client = real_http_client(transport=httpx.MockTransport(upstream), **kwargs)
        owned_clients.append(client)
        return client

    async def measure():
        nonlocal upstream_delay_seconds
        pending = []
        completed = []
        active = 0
        max_active = 0

        class TrackRequests:
            async def __call__(self, scope, receive, send):
                nonlocal active, max_active
                active += 1
                max_active = max(max_active, active)
                started = time.perf_counter()
                try:
                    return await app(scope, receive, send)
                finally:
                    active -= 1
                    completed.append(time.perf_counter() - started)

        async def get(http, path):
            started = time.perf_counter()
            response = await http.get(path)
            elapsed = time.perf_counter() - started
            expected = "conversations" if path.endswith("conversations") else "integrations"
            if response.status_code != 200 or len(response.json().get(expected, [])) != 1:
                raise AssertionError("Synthetic GET response changed")
            return elapsed

        with patch.object(supabase_service, "_configuration", return_value=(
            "https://synthetic.example.invalid", "synthetic-service-key"
        )), patch.object(supabase_service, "_new_http_client", side_effect=new_client):
            startup_at = time.perf_counter()
            async with app.router.lifespan_context(app):
                startup_seconds = time.perf_counter() - startup_at
                sdk_loaded_after_startup = "supabase" in sys.modules
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=TrackRequests()),
                    base_url="http://synthetic-asgi.invalid",
                    headers={"Authorization": "Bearer synthetic-user-token"},
                ) as http:
                    first_seconds = await get(http, PATHS[0])
                    sdk_loaded_after_first_get = "supabase" in sys.modules
                    warm = {path: [] for path in PATHS}
                    for _ in range(args.warm_samples):
                        for path in PATHS:
                            warm[path].append(await get(http, path))

                    # This deliberately exceeds the browser's window but is
                    # within the unchanged per-phase Auth/DB timeout settings
                    # when interpreted on the unscaled (8 + 8 second) clock.
                    # MockTransport does not enforce HTTPX timeout extensions.
                    upstream_delay_seconds = 8.0 * args.retry_scale
                    slow_baseline_seconds = await get(http, PATHS[0])
                    completed.clear()
                    max_active = 0

                    async def retry_loader(path):
                        attempts = []
                        started = time.perf_counter()
                        for index in range(6):
                            task = asyncio.create_task(get(http, path))
                            pending.append(task)
                            try:
                                await asyncio.wait_for(
                                    asyncio.shield(task),
                                    timeout=ATTEMPT_SECONDS * args.retry_scale,
                                )
                            except asyncio.TimeoutError:
                                attempts.append("frontend-timeout")
                            else:
                                attempts.append("success")
                                return {
                                    "attempts": attempts,
                                    "elapsed_seconds": time.perf_counter() - started,
                                }
                            if index < 5:
                                await asyncio.sleep(BACKOFF_SECONDS[index] * args.retry_scale)
                        return {
                            "attempts": attempts,
                            "elapsed_seconds": time.perf_counter() - started,
                        }

                    retry_started = time.perf_counter()
                    try:
                        loaders = await asyncio.gather(*(retry_loader(path) for path in PATHS))
                        frontend_seconds = time.perf_counter() - retry_started
                        requests_running_at_frontend_exit = active
                    finally:
                        # A browser abort may leave synchronous worker I/O
                        # running. Shield reproduces that case; do not close
                        # shared pools until all simulated requests drain.
                        await asyncio.gather(*pending)
                    drained_seconds = time.perf_counter() - retry_started
                synthetic_upstream_calls = sum(counts.values())
            all_clients_closed = bool(owned_clients) and all(c.is_closed for c in owned_clients)

        if blocked_calls or active or not all_clients_closed:
            raise AssertionError("Network, request-drain or resource-cleanup invariant failed")
        return {
            "import_seconds": import_seconds,
            "lifespan_startup_seconds": startup_seconds,
            "supabase_imported_after_app_import": sdk_loaded_after_import,
            "supabase_imported_after_lifespan_startup": sdk_loaded_after_startup,
            "supabase_imported_after_first_get": sdk_loaded_after_first_get,
            "first_authenticated_conversations_get_seconds": first_seconds,
            "warm_get_seconds": warm,
            "retry_simulation": {
                "scale": args.retry_scale,
                "synthetic_auth_and_database_delay_seconds_each": upstream_delay_seconds,
                "slow_authenticated_get_seconds": slow_baseline_seconds,
                "window_seconds": ATTEMPT_SECONDS * args.retry_scale,
                "frontend_elapsed_seconds": frontend_seconds,
                "drained_elapsed_seconds": drained_seconds,
                "loaders": loaders,
                "backend_completed_requests": len(completed),
                "backend_requests_running_at_frontend_exit": requests_running_at_frontend_exit,
                "max_overlapping_backend_requests": max_active,
            },
            "synthetic_upstream_calls": synthetic_upstream_calls,
            "owned_http_clients_created": len(owned_clients),
            "all_owned_http_clients_closed_after_drain": all_clients_closed,
            "blocked_network_attempts": blocked_calls,
        }

    return asyncio.run(measure())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--warm-samples", type=int, default=5)
    parser.add_argument("--upstream-delay-ms", type=float, default=5.0)
    parser.add_argument("--retry-scale", type=float, default=0.01)
    parser.add_argument("--_worker", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if not 1 <= args.runs <= 20 or not 1 <= args.warm_samples <= 100:
        parser.error("runs must be 1..20 and warm-samples must be 1..100")
    if not 0 <= args.upstream_delay_ms <= 100 or not 0.005 <= args.retry_scale <= 0.1:
        parser.error("upstream-delay-ms must be 0..100 and retry-scale must be 0.005..0.1")
    if args._worker:
        print(json.dumps(_worker(args)))
        return

    # Deliberately do not inherit credential bindings, proxy settings, dotenv
    # configuration or environment customization from the calling process.
    env = {
        "PYTHONPATH": str(BACKEND),
        "PYTHON_DOTENV_DISABLED": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PHILOMATH_PERFORMANCE_LOGGING": "false",
        "PATH": os.defpath,
    }
    samples = []
    for _ in range(args.runs):
        result = subprocess.run(
            [sys.executable, "-B", str(Path(__file__).resolve()), "--_worker",
             "--warm-samples", str(args.warm_samples),
             "--upstream-delay-ms", str(args.upstream_delay_ms),
             "--retry-scale", str(args.retry_scale)],
            env=env, cwd=BACKEND, check=True, capture_output=True, text=True,
            timeout=60,
        )
        samples.append(json.loads(result.stdout))
    print(json.dumps({
        "method": "Fresh processes; real FastAPI and Supabase SDK; synthetic MockTransport; sockets blocked.",
        "artificial_upstream_delay_seconds_per_auth_or_database_call": args.upstream_delay_ms / 1000,
        "import": _summary([s["import_seconds"] for s in samples]),
        "lifespan_startup": _summary([s["lifespan_startup_seconds"] for s in samples]),
        "first_authenticated_conversations_get": _summary([
            s["first_authenticated_conversations_get_seconds"] for s in samples
        ]),
        "warm_get": {
            path: _summary([value for s in samples for value in s["warm_get_seconds"][path]])
            for path in PATHS
        },
        "retry_policy_unscaled_seconds": {
            "per_attempt_window": ATTEMPT_SECONDS,
            "attempts": 6,
            "backoffs": BACKOFF_SECONDS,
            "all_timeout_elapsed": 6 * ATTEMPT_SECONDS + sum(BACKOFF_SECONDS),
        },
        "supabase_import_observations": {
            "after_app_import": [s["supabase_imported_after_app_import"] for s in samples],
            "after_startup": [s["supabase_imported_after_lifespan_startup"] for s in samples],
            "after_first_get": [s["supabase_imported_after_first_get"] for s in samples],
        },
        "retry_simulation_runs": [s["retry_simulation"] for s in samples],
        "resource_checks": [{
            key: s[key] for key in (
                "synthetic_upstream_calls", "owned_http_clients_created",
                "all_owned_http_clients_closed_after_drain", "blocked_network_attempts",
            )
        } for s in samples],
        "limitations": [
            "No production latency, DNS, TLS, real connection-pool, database-plan or provider measurements.",
            "First GET includes lazy Supabase import; startup-only time shifts work to first use.",
            "MockTransport sleeps model upstream waiting but do not exercise HTTPX timeout enforcement.",
            "Scaled simulator retains six windows and backoffs; scheduler overhead does not scale.",
            "Shielded ASGI tasks model browser aborts that leave server work running; no browser or socket is used.",
            "Simulator waits for the tiny complete JSON body; the real frontend clears its timer at headers.",
            "The unchanged endpoint budget and 401-on-upstream-auth-error policies are not fixed here.",
            "Render Free cold starts remain outside these measurements.",
        ],
    }, indent=2))


if __name__ == "__main__":
    main()
