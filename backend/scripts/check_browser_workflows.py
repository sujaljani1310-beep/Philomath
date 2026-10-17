"""Local Chromium workflows; synthetic OAuth/Auth/DB/AI only, never staging.

Run with a Python environment that already provides Playwright. The backend
uses backend/.venv, and the frontend uses its existing node_modules. Builds and
logs live in a temporary directory. --offline-fonts is a documented font fixture,
not a successful real Google Fonts download or visual regression test.
"""
import argparse
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from urllib.parse import parse_qs, urlsplit
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend"
FRONTEND = ROOT / "frontend"
UI = "http://127.0.0.1:8767"
API = "http://127.0.0.1:8765"
AUTH = "http://127.0.0.1:8766"
ANSWER = "Synthetic provider answer: the application completed this isolated test successfully."


def environment():
    return {"PATH": os.environ.get("PATH", os.defpath), "PYTHONPATH": os.pathsep.join(sys.path),
            "PYTHONDONTWRITEBYTECODE": "1", "PYTHON_DOTENV_DISABLED": "1",
            "NEXT_TELEMETRY_DISABLED": "1", "PHILOMATH_PERFORMANCE_LOGGING": "false",
            "NEXT_PUBLIC_BACKEND_URL": API, "NEXT_PUBLIC_SUPABASE_URL": AUTH,
            "NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY": "synthetic-public-key",
            "FRONTEND_ORIGINS": UI, "NODE_ENV": "production"}


def ready(url, process):
    until = time.monotonic() + 30
    while time.monotonic() < until:
        if process.poll() is not None:
            raise RuntimeError("Synthetic server exited; inspect its local log")
        try:
            with urlopen(url, timeout=0.5) as response:
                if response.status == 200:
                    return
        except OSError:
            time.sleep(0.05)
    raise TimeoutError("Loopback server readiness timed out")


def build(directory, args, env):
    revision = None
    if args.baseline_ref:
        revision = subprocess.check_output(["git", "rev-parse", "--verify", "--end-of-options",
            args.baseline_ref + "^{commit}"], cwd=ROOT, text=True).strip()
        paths = subprocess.check_output(["git", "ls-tree", "-r", "--name-only", revision,
            "--", "frontend"], cwd=ROOT, text=True).splitlines()
    else:
        paths = subprocess.check_output(["git", "ls-files", "frontend"], cwd=ROOT, text=True).splitlines()
    for name in paths:
        relative = Path(name).relative_to("frontend")
        if relative.name.startswith(".env"):
            continue
        target = directory / relative
        if not target.resolve().is_relative_to(directory.resolve()):
            raise ValueError("Unapproved frontend source path")
        target.parent.mkdir(parents=True, exist_ok=True)
        content = subprocess.check_output(["git", "show", revision + ":" + name], cwd=ROOT) if revision else (ROOT / name).read_bytes()
        target.write_bytes(content)
    (directory / "node_modules").symlink_to(FRONTEND / "node_modules", target_is_directory=True)
    if args.offline_fonts:
        font = Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf")
        if not font.is_file():
            raise RuntimeError("Offline font fixture is unavailable; use a network-enabled normal build")
        css = "@font-face {font-family:'Inter';font-style:normal;font-weight:100 900;src:url(" + str(font) + ") format('truetype');}"
        mock = directory / "font-fixture.cjs"
        mock.write_text('module.exports = new Proxy({}, {get: (_, url) => typeof url === "string" && url.startsWith("https://fonts.googleapis.com/css2?family=Inter") ? ' + json.dumps(css) + ' : undefined});\n')
        env["NEXT_FONT_GOOGLE_MOCKED_RESPONSES"] = str(mock)
    began = time.perf_counter()
    with (directory / "build.log").open("w") as log:
        completed = subprocess.run(["node", "node_modules/next/dist/bin/next", "build", "--webpack"],
            cwd=directory, env=env, stdout=log, stderr=subprocess.STDOUT, timeout=120)
    if completed.returncode:
        raise RuntimeError("Frontend build failed; inspect local build.log")
    return {"seconds": round(time.perf_counter() - began, 3), "baseline_commit": revision,
            "font_fixture": args.offline_fonts, "bundler": "webpack"}


def browser_checks(args):
    from playwright.sync_api import sync_playwright, expect
    seen = []
    blocked = []
    errors = []
    injected = {"fail_next_chat": False}
    user = {"id": "11111111-1111-4111-8111-111111111111", "aud": "authenticated",
            "role": "authenticated", "email": "synthetic-user-a@example.invalid",
            "app_metadata": {"provider": "google"}, "user_metadata": {}, "created_at": "2026-01-01T00:00:00Z"}
    results = {}
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=shutil.which("chromium"), headless=True,
                                   args=["--no-sandbox", "--disable-background-networking"])
        try:
            context = browser.new_context(viewport={"width": 1440, "height": 1000})
            def route(request_route):
                request = request_route.request
                url = urlsplit(request.url)
                origin = f"{url.scheme}://{url.netloc}"
                if origin == AUTH:
                    seen.append((request.method, url.path))
                    if url.path == "/auth/v1/authorize":
                        assert parse_qs(url.query)["provider"] == ["google"]
                        assert parse_qs(url.query)["redirect_to"] == [UI]
                        request_route.fulfill(status=302, headers={"Location": UI +
                            "/#access_token=synthetic-user-a&refresh_token=synthetic-refresh&expires_in=3600&token_type=bearer"})
                    elif url.path == "/auth/v1/user":
                        request_route.fulfill(status=200, json=user)
                    elif url.path == "/auth/v1/logout":
                        request_route.fulfill(status=204, body="")
                    else:
                        raise AssertionError("Unexpected synthetic Auth path")
                elif origin in (UI, API):
                    if origin == API:
                        seen.append((request.method, url.path))
                        if injected["fail_next_chat"] and url.path == "/api/chat/send":
                            injected["fail_next_chat"] = False
                            request_route.fulfill(status=503, json={"detail": "Synthetic unavailable"},
                                headers={"Access-Control-Allow-Origin": UI})
                            return
                    request_route.continue_()
                else:
                    blocked.append(origin)
                    request_route.abort()
            context.route("**/*", route)
            page = context.new_page()
            page.on("pageerror", lambda error: errors.append(type(error).__name__))
            start = time.perf_counter()
            page.goto(UI, wait_until="domcontentloaded")
            expect(page.get_by_role("button", name="Continue with Google")).to_be_visible()
            results["signed_out_ready_ms"] = round((time.perf_counter() - start) * 1000, 3)
            with page.expect_response(lambda r: r.url.startswith(API + "/api/integrations") and r.request.method == "GET"):
                page.get_by_role("button", name="Continue with Google").click()
            expect(page.get_by_role("dialog")).to_be_visible()
            results["synthetic_google_oauth_handoff"] = "PASS"
            def add_ai(name):
                page.get_by_placeholder("Gemini, OpenRouter, Cerebras, NVIDIA, or Grok").fill(name)
                page.get_by_placeholder("Paste API key").fill("synthetic-byok-key-" + name)
                page.get_by_role("dialog").get_by_role("button", name="Add AI", exact=True).click()
                expect(page.get_by_role("dialog")).not_to_be_visible()
            add_ai("openrouter")
            page.get_by_role("button", name="Show Chats", exact=True).click()
            before = len([x for x in seen if x[0] == "GET" and x[1].startswith("/api/conversations/")])
            page.get_by_role("button", name="New Chat", exact=True).click()
            page.wait_for_timeout(600)
            after = len([x for x in seen if x[0] == "GET" and x[1].startswith("/api/conversations/")])
            results["new_unsaved_chat_history_gets_in_600ms"] = after - before
            if args.expect_legacy:
                assert after > before, "Legacy history-loop reproduction did not trigger"
                return {**results, "blocked_external_origins": sorted(set(blocked)), "mode": "legacy-reproduction"}
            assert after == before, "Unsaved chat unexpectedly fetched persisted history"
            def send(message, placeholder, status=200):
                field = page.get_by_placeholder(placeholder)
                field.fill(message)
                with page.expect_response(lambda r: r.url == API + "/api/chat/send" and r.request.method == "POST") as info:
                    field.press("Enter")
                response = info.value
                assert response.status == status, f"Unexpected synthetic chat status {response.status}"
                expect(field).to_be_enabled()
                if status == 200:
                    expect(page.get_by_text(ANSWER, exact=True).last).to_be_visible()
                    return response.json()
                return None
            first = send("Explain browser example.", "Ask me anything...")
            results["basic_chat_and_byok"] = "PASS"
            def mode(label):
                page.get_by_role("button", name="Mode", exact=True).click()
                page.get_by_role("menuitem").filter(has_text=label).click()
            mode("Manual Mode")
            page.get_by_role("button", name="OpenRouter", exact=False).first.click()
            page.locator('input[type="file"]').set_input_files({"name": "synthetic.txt", "mimeType": "text/plain", "buffer": b"Synthetic browser file evidence."})
            send("Use the attached text.", "Select a provider above, then ask your question...")
            results["manual_chat_and_upload"] = "PASS"
            page.get_by_role("button", name="Add AI", exact=True).click()
            add_ai("gemini")
            page.get_by_role("button", name="Gemini", exact=False).first.click()
            switched = send("Explain a second example.", "Select a provider above, then ask your question...")
            assert switched["provider_used"] == "gemini"
            results["provider_switch"] = "PASS"
            mode("Auto Mode")
            injected["fail_next_chat"] = True
            send("Synthetic temporary failure.", "Ask anything - AI will auto-select the best model...", 503)
            page.wait_for_timeout(150)
            recovered = send("Explain fractions.", "Ask anything - AI will auto-select the best model...")
            assert recovered["mode_used"] == "auto" and "routing" in recovered
            results["auto_chat_and_error_recovery"] = "PASS"
            start = time.perf_counter()
            page.reload(wait_until="domcontentloaded")
            expect(page.get_by_role("button", name="Show Chats", exact=True)).to_be_visible()
            results["authenticated_reload_ready_ms"] = round((time.perf_counter() - start) * 1000, 3)
            page.get_by_role("button", name="Show Chats", exact=True).click()
            page.get_by_text("Explain browser example.", exact=True).click()
            expect(page.get_by_text(ANSWER, exact=True).first).to_be_visible()
            history = context.request.get(API + "/api/conversations/" + first["conversation_id"],
                headers={"Authorization": "Bearer synthetic-user-a"}).json()["messages"]
            assert len(history) == 8
            results["persisted_history_after_reload"] = "PASS"
            metrics = context.new_cdp_session(page)
            metrics.send("Performance.enable")
            values = {m["name"]: m["value"] for m in metrics.send("Performance.getMetrics")["metrics"]}
            results["browser_js_heap_used_mib"] = round(values["JSHeapUsedSize"] / 1024**2, 3)
            results["javascript_transfer_bytes_on_reload"] = page.evaluate("performance.getEntriesByType('resource').filter(e=>e.name.includes('.js')).reduce((n,e)=>n+e.transferSize,0)")
            page.locator('button:has(svg.lucide-settings)').click()
            page.get_by_role("button", name="Sign out", exact=True).click()
            expect(page.get_by_role("button", name="Continue with Google")).to_be_visible()
            results["sign_out"] = "PASS"
            assert not blocked, "Browser attempted an unexpected external origin"
            assert not errors, "Browser raised an unhandled page error"
            results["browser_page_errors"] = len(errors)
            results["chat_post_attempts_including_injected_503"] = sum(x == ("POST", "/api/chat/send") for x in seen)
            return results
        finally:
            browser.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--offline-fonts", action="store_true")
    parser.add_argument("--baseline-ref")
    parser.add_argument("--expect-legacy", action="store_true")
    parser.add_argument("--built-directory", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--_isolated", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if not args._isolated:
        return subprocess.run([sys.executable, "-B", str(Path(__file__).resolve()), *sys.argv[1:], "--_isolated"], env=environment()).returncode
    if not shutil.which("chromium"):
        raise SystemExit("Existing Chromium is required; no dependencies are installed by this script")
    directory = args.built_directory or Path(tempfile.mkdtemp(prefix="philomath-browser-"))
    env = environment()
    built = {"reused_temporary_build": True, "font_fixture": args.offline_fonts} if args.built_directory else build(directory, args, env)
    processes = []
    logs = []
    try:
        for label, command, cwd in (
            ("backend", [str(BACKEND / ".venv/bin/python"), "-B", str(BACKEND / "scripts/workflow_server.py")], BACKEND),
            ("frontend", ["node", "node_modules/next/dist/bin/next", "start", "-H", "127.0.0.1", "-p", "8767"], directory),
        ):
            log = (directory / (label + ".log")).open("w")
            logs.append(log)
            child_env = dict(env)
            if label == "backend":
                child_env["PYTHONPATH"] = str(BACKEND)
                for key in list(child_env):
                    if "SUPABASE" in key:
                        child_env.pop(key)
            process = subprocess.Popen(command, cwd=cwd, env=child_env, stdout=log, stderr=subprocess.STDOUT,
                                       start_new_session=True)
            processes.append(process)
            ready(API + "/" if label == "backend" else UI, process)
        result = browser_checks(args)
    finally:
        for process in reversed(processes):
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=8)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=3)
        for log in logs:
            log.close()
    cleanup = json.loads((directory / "backend.log").read_text().splitlines()[-1])
    assert cleanup == {"synthetic_server_closed": True, "active_leases": 0,
                       "database_pool_closed": True, "async_pools_closed": True}, "Synthetic cleanup incomplete"
    print(json.dumps({"build": built, "checks": result, "cleanup": cleanup,
                      "local_artifacts": str(directory),
                      "live_google_or_provider_services_tested": False}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
