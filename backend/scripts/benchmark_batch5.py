"""Fresh-process, socket-blocked SDK/ASGI latency and RSS measurements.

All Auth/DB/provider traffic uses real SDKs against synthetic HTTPX peers. The
five-millisecond provider delay is artificial. This excludes real service speed,
hosting, TLS, databases, billing and real Google authentication.
"""
import argparse
import json
import os
from pathlib import Path
import statistics
import subprocess
import sys

BACKEND = Path(__file__).resolve().parents[1]


def worker(samples):
    from collections import defaultdict
    import resource
    import socket
    import time
    from unittest.mock import patch

    def blocked(*_args, **_kwargs):
        raise AssertionError("Network access is forbidden in this benchmark")
    socket.socket.connect = socket.socket.connect_ex = socket.getaddrinfo = blocked

    def rss():
        path = Path('/proc/self/status')
        if path.exists():
            return round(int(next(line.split()[1] for line in path.read_text().splitlines()
                                  if line.startswith('VmRSS:'))) / 1024, 3)
        return None

    began = time.perf_counter()
    from app.main import app
    import_seconds = time.perf_counter() - began
    after_import_rss = rss()
    import httpx
    from app import observability
    from app.services import supabase_service

    def fake_http(**kwargs):
        return httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=[])), **kwargs)
    with patch.object(supabase_service, "_configuration", return_value=("https://synthetic.invalid", "synthetic-key")), \
         patch.object(supabase_service, "_new_http_client", side_effect=fake_http):
        began = time.perf_counter()
        supabase_service.require_supabase()
        first_sdk_seconds = time.perf_counter() - began
        after_sdk_rss = rss()
        supabase_service.close_supabase_client()

    from tests.workflow_fixture import WorkflowFixture
    from app.services.brain import router_brain
    from app.services.brain.context import RoutingContext
    from app.services.brain.model_registry import MODEL_REGISTRY
    spans = defaultdict(list)
    def record(operation, started, **_fields):
        spans[operation].append((time.perf_counter() - started) * 1000)

    with patch.object(observability, "record_timing", side_effect=record), WorkflowFixture() as f:
        f.provider_delay = 0.005
        f.add_provider("openrouter")
        first_spans = {}
        def call(method, path, **kwargs):
            began = time.perf_counter()
            response = f.request(method, path, **kwargs)
            if response.status_code != 200:
                raise AssertionError("Synthetic benchmark operation failed: " + str(response.status_code))
            return (time.perf_counter() - began) * 1000
        before = f.counts.copy()
        first_chat = call("POST", "/api/chat/send", json={"message": "Explain.", "mode": "manual",
                         "provider": "openrouter", "conversation_id": "first-chat"})
        first_spans = {name: round(values[-1], 3) for name, values in spans.items() if name.startswith(("db.", "provider."))}
        chat_upstream = {method + " " + path: count for (method,path),count in (f.counts - before).items()}
        after_first_chat_rss = rss()
        warm = defaultdict(list)
        for sample in range(samples):
            f.now += 60  # Exclude admission waiting; production policies are unchanged.
            for path in ("/api/conversations", "/api/integrations", "/api/conversations/first-chat"):
                warm[path].append(call("GET", path))
            warm["manual_chat"].append(call("POST", "/api/chat/send", json={"message": "Explain.",
                "mode": "manual", "provider": "openrouter", "conversation_id": "warm-" + str(sample)}))
        f.now += 60
        f.add_provider("gemini")
        first_gemini = call("POST", "/api/chat/send", json={"message": "Explain.", "mode": "manual",
                            "provider": "gemini", "conversation_id": "gemini-first"})
        after_gemini_rss = rss()
        warm_provider_posts = len(f.provider_calls)
    cleanup = {"leases": f.controller.active, "database_pool_closed": f.db.http.is_closed,
               "async_pools_closed": all(client.is_closed for client in f.async_clients)}
    audit = []
    messages = ["Explain a simple idea.", "Write a Python function.", "Solve 12 + 34.",
                "Write a creative story.", "Find the latest research.", "Summarize this text.",
                "Fix this python API function exception error using the latest version in the attached document."]
    for message in messages:
        with patch.object(router_brain, "classify_ambiguous_request", return_value=None) as classifier:
            began = time.perf_counter()
            decision = router_brain.route(RoutingContext(message), available_provider_keys=MODEL_REGISTRY,
                                          classifier_api_key="synthetic-only-classifier-key")
            elapsed = (time.perf_counter() - began) * 1000
        audit.append({"message": message, "category": decision.dominant_category,
                      "ranked": [{"provider": model.key, "score": round(model.score, 4)} for model in decision.ranked],
                      "classifier_requested": classifier.called, "routing_cpu_ms": round(elapsed, 3)})
    return {"app_import_seconds": round(import_seconds, 6),
            "first_supabase_sdk_initialization_seconds": round(first_sdk_seconds, 6),
            "first_openai_sdk_chat_ms": round(first_chat, 3), "first_gemini_sdk_chat_ms": round(first_gemini, 3),
            "rss_mib": {"after_import": after_import_rss, "after_supabase_sdk": after_sdk_rss,
                        "after_first_chat": after_first_chat_rss, "after_gemini": after_gemini_rss},
            "warm_ms": {name: {"samples": [round(value,3) for value in values],
                "median": round(statistics.median(values),3), "max": round(max(values),3)} for name,values in warm.items()},
            "first_chat_upstream_requests": chat_upstream, "first_chat_spans_ms": first_spans,
            "synthetic_provider_posts": warm_provider_posts, "cleanup": cleanup, "auto_audit": audit}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--samples", type=int, default=10)
    parser.add_argument("--_worker", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if not 1 <= args.runs <= 10 or not 1 <= args.samples <= 30:
        parser.error("runs: 1..10; samples: 1..30")
    if args._worker:
        print(json.dumps(worker(args.samples)))
        return
    env = {"PATH": os.defpath, "PYTHONPATH": str(BACKEND), "PYTHONDONTWRITEBYTECODE": "1",
           "PYTHON_DOTENV_DISABLED": "1", "PHILOMATH_PERFORMANCE_LOGGING": "false"}
    results = []
    for _ in range(args.runs):
        child = subprocess.run([sys.executable, "-B", str(Path(__file__).resolve()), "--_worker",
            "--samples", str(args.samples)], cwd=BACKEND, env=env, text=True, capture_output=True, check=True)
        results.append(json.loads(child.stdout))
    print(json.dumps({"runs": results, "limitations": __doc__}, indent=2))


if __name__ == "__main__":
    main()
