"""Offline startup timings and the unchanged frontend retry schedule.

Uses synthetic HTTPX async transports, no real credentials or network. The
scaled outage models an eight-second auth response, the backend's four-second
phase cap and ten-second total budget, and the frontend's twelve-second window
plus six attempts/backoffs. DNS, TLS, actual database work, hosting cold starts,
and deployment-proxy queues are excluded. Measurements do not predict production.
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
RETRYABLE = {408, 425, 429, 500, 502, 503, 504}
BACKOFF = (1.5, 3.0, 6.0, 8.0, 8.0)


def worker(args):
    import asyncio
    from collections import Counter
    import socket
    import time
    from unittest.mock import patch

    def blocked(*_args, **_kwargs):
        raise AssertionError("Network access is forbidden in this benchmark")

    socket.socket.connect = blocked
    socket.socket.connect_ex = blocked
    socket.getaddrinfo = blocked
    socket.create_connection = blocked

    started = time.perf_counter()
    from app.main import app
    imported = time.perf_counter() - started
    sdk_after_import = "supabase" in sys.modules

    import httpx
    from app.http_safety import stateless_cookies
    from app import request_limits
    from app.services import startup_service

    counts = Counter()
    upstream_active = 0
    max_upstream_active = 0
    stopped_upstream = 0
    delay = 0.005
    owned = []

    async def upstream(request):
        nonlocal upstream_active, max_upstream_active, stopped_upstream
        if request.url.host != "synthetic.example.invalid" or request.method != "GET":
            raise AssertionError("Unexpected upstream request")
        counts[request.url.path] += 1
        upstream_active += 1
        max_upstream_active = max(max_upstream_active, upstream_active)
        try:
            await asyncio.sleep(delay)
            if request.url.path == "/auth/v1/user":
                if request.headers["authorization"] != "Bearer synthetic-user-token":
                    raise AssertionError("Unexpected bearer token")
                return httpx.Response(200, json={"id": "synthetic-owner"})
            if request.url.params["user_id"] != "eq.synthetic-owner":
                raise AssertionError("Missing ownership filter")
            if request.headers["authorization"] != "Bearer synthetic-service-key":
                raise AssertionError("Unexpected database credential")
            if request.url.path == "/rest/v1/conversations":
                return httpx.Response(200, json=[{"id": "chat", "title": "Synthetic"}])
            if request.url.path == "/rest/v1/user_ai_integrations":
                return httpx.Response(200, json=[{"provider": "gemini", "api_key_last4": "0000"}])
            raise AssertionError("Unexpected upstream path")
        except asyncio.CancelledError:
            stopped_upstream += 1
            raise
        finally:
            upstream_active -= 1

    def new_client():
        client = httpx.AsyncClient(
            transport=httpx.MockTransport(upstream), cookies=stateless_cookies(),
            timeout=httpx.Timeout(4, connect=2, pool=0.5),
            limits=httpx.Limits(max_connections=64, max_keepalive_connections=16),
            follow_redirects=False,
            event_hooks={"response": [startup_service._redirect_guard]},
        )
        owned.append(client)
        return client

    async def measure():
        nonlocal delay
        actual_init = request_limits.RequestLimitsMiddleware.__init__

        def scaled_init(instance, wrapped, **kwargs):
            kwargs["startup_deadline"] = 10 * args.scale
            actual_init(instance, wrapped, **kwargs)

        with patch.object(startup_service, "_configuration", return_value=(
            "https://synthetic.example.invalid", "synthetic-service-key",
        )), patch.object(startup_service, "_new_http_client", side_effect=new_client), patch.object(
            startup_service, "STARTUP_PHASE_SECONDS", 4 * args.scale,
        ), patch.object(request_limits.RequestLimitsMiddleware, "__init__", scaled_init):
            startup_at = time.perf_counter()
            async with app.router.lifespan_context(app):
                startup_seconds = time.perf_counter() - startup_at
                sdk_after_startup = "supabase" in sys.modules
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app),
                    base_url="http://synthetic-asgi.invalid",
                    headers={"Authorization": "Bearer synthetic-user-token"},
                ) as http:
                    async def get(path):
                        began = time.perf_counter()
                        response = await http.get(path)
                        return response, time.perf_counter() - began

                    response, first_seconds = await get(PATHS[0])
                    if response.status_code != 200:
                        raise AssertionError("First synthetic request failed")
                    sdk_after_first_get = "supabase" in sys.modules
                    warm = {path: [] for path in PATHS}
                    for _ in range(args.warm_samples):
                        for path in PATHS:
                            response, elapsed = await get(path)
                            if response.status_code != 200:
                                raise AssertionError("Warm synthetic request failed")
                            warm[path].append(elapsed)

                    # Eight seconds per phase would outlive the browser's
                    # request window in the old sequential auth + DB path.
                    # The new four-second auth cap stops each attempt first.
                    delay = 8 * args.scale
                    counts.clear()

                    async def frontend_loader(path):
                        attempts = []
                        began = time.perf_counter()
                        for index in range(6):
                            try:
                                response, elapsed = await asyncio.wait_for(get(path), 12 * args.scale)
                            except asyncio.TimeoutError:
                                attempts.append({"result": "frontend-timeout"})
                            else:
                                attempts.append({"status": response.status_code, "seconds": round(elapsed, 6)})
                                if response.is_success or response.status_code not in RETRYABLE:
                                    break
                            if index < 5:
                                await asyncio.sleep(BACKOFF[index] * args.scale)
                        return {"path": path, "attempts": attempts, "seconds": round(time.perf_counter() - began, 6)}

                    began = time.perf_counter()
                    loaders = await asyncio.gather(*(frontend_loader(path) for path in PATHS))
                    outage_seconds = time.perf_counter() - began
                    active_at_exit = upstream_active
                    delay = 0
                    response, recovery_seconds = await get(PATHS[0])
                    if response.status_code != 200:
                        raise AssertionError("Pool did not recover after cancellation")
            clients_closed = bool(owned) and all(client.is_closed for client in owned)

        if active_at_exit or upstream_active or not clients_closed:
            raise AssertionError("Upstream cancellation or cleanup failed")
        return {
            "app_import_seconds": round(imported, 6),
            "lifespan_startup_seconds": round(startup_seconds, 6),
            "first_authenticated_get_seconds": round(first_seconds, 6),
            "supabase_imported_after_app_import": sdk_after_import,
            "supabase_imported_after_startup": sdk_after_startup,
            "supabase_imported_after_first_get": sdk_after_first_get,
            "warm_get_seconds": {path: [round(value, 6) for value in values] for path, values in warm.items()},
            "retry_simulation": {
                "scale": args.scale,
                "backend_phase_seconds": 4 * args.scale,
                "backend_total_seconds": 10 * args.scale,
                "frontend_attempt_seconds": 12 * args.scale,
                "upstream_response_seconds": 8 * args.scale,
                "elapsed_seconds": round(outage_seconds, 6),
                "original_frontend_worst_case_seconds": 98.5 * args.scale,
                "loaders": loaders,
                "upstream_active_at_frontend_exit": active_at_exit,
                "max_upstream_active": max_upstream_active,
                "cancelled_upstream_operations": stopped_upstream,
                "upstream_requests_including_recovery": dict(counts),
            },
            "recovery_seconds": round(recovery_seconds, 6),
            "all_owned_clients_closed": clients_closed,
        }

    return asyncio.run(measure())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--warm-samples", type=int, default=3)
    parser.add_argument("--scale", type=float, default=0.01)
    parser.add_argument("--_worker", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if not 1 <= args.runs <= 10 or not 1 <= args.warm_samples <= 3 or not 0.005 <= args.scale <= 0.05:
        parser.error("runs: 1..10, warm-samples: 1..3, scale: 0.005..0.05")
    if args._worker:
        print(json.dumps(worker(args)))
        return
    environment = {
        "PATH": os.defpath, "PYTHONPATH": str(BACKEND), "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHON_DOTENV_DISABLED": "1", "PHILOMATH_PERFORMANCE_LOGGING": "false",
    }
    samples = []
    for _ in range(args.runs):
        completed = subprocess.run([
            sys.executable, "-B", str(Path(__file__).resolve()), "--_worker",
            "--warm-samples", str(args.warm_samples), "--scale", str(args.scale),
        ], cwd=BACKEND, env=environment, text=True, capture_output=True, check=True)
        samples.append(json.loads(completed.stdout))
    print(json.dumps({
        "samples": samples,
        "median_seconds": {
            key: round(statistics.median(sample[key] for sample in samples), 6)
            for key in ("app_import_seconds", "lifespan_startup_seconds", "first_authenticated_get_seconds", "recovery_seconds")
        },
        "limitations": "Synthetic async transports exclude hosting, DNS, TLS, sockets, real database work, and deployment-proxy queues. The frontend retry count/backoffs are preserved; sustained failures still trigger six attempts.",
    }, indent=2))


if __name__ == "__main__":
    main()
