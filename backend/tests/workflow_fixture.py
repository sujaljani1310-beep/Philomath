"""Synthetic ASGI workflow peer using the actual installed Auth/DB/AI SDKs.

No dependency overrides: bearer validation, encryption, persistence and provider
parsing execute normally. All upstream I/O is intercepted; no hosted service or
real OAuth identity is involved. This is not a PostgreSQL/RLS emulator.
"""
from collections import Counter
from contextlib import ExitStack
import json
import os
from threading import Lock
import time
from unittest.mock import patch

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
import httpx

os.environ["PYTHON_DOTENV_DISABLED"] = "1"

from app.http_safety import stateless_cookies
from app.main import app
from app.providers import clients
from app import request_limits
from app.services import startup_service, supabase_service
from tests.synthetic_database import SyntheticDatabase


ANSWER = "Synthetic provider answer: the application completed this isolated test successfully."
HOSTS = {"openrouter.ai": "openrouter", "api.cerebras.ai": "cerebras",
         "integrate.api.nvidia.com": "nvidia", "api.x.ai": "grok",
         "generativelanguage.googleapis.com": "gemini"}
USER_IDS = {"synthetic-user-a": "11111111-1111-4111-8111-111111111111",
            "synthetic-user-b": "22222222-2222-4222-8222-222222222222"}


class WorkflowFixture:
    def __init__(self):
        self.stack = ExitStack()
        self.db = None
        self.provider_calls = []
        self.provider_failures = {}
        self.auth_failure = None
        self.database_fault = lambda phase, request, body: None
        self.provider_delay = 0.0
        self.now = 0.0
        self.counts = Counter()
        self.guard = Lock()
        self.async_clients = []

    def database_hook(self, phase, request, body):
        if phase == "before":
            with self.guard:
                self.counts[(request.method, request.url.path)] += 1
            if request.url.path == "/auth/v1/user":
                token = request.headers.get("authorization", "").removeprefix("Bearer ")
                if self.auth_failure:
                    return httpx.Response(self.auth_failure, json={"message": "Synthetic upstream failure"})
                if token not in USER_IDS:
                    return httpx.Response(401, json={"code": "bad_jwt", "message": "Synthetic invalid session"})
                return httpx.Response(200, json={"id": USER_IDS[token], "aud": "authenticated",
                    "role": "authenticated", "email": token + "@example.invalid",
                    "app_metadata": {"provider": "google"}, "user_metadata": {},
                    "created_at": "2026-01-01T00:00:00Z"})
        result = self.database_fault(phase, request, body)
        if result is not None:
            return result
        if phase == "after" and request.method == "POST":
            # Supply ordinary default timestamps, not identities from any database.
            with self.db.lock:
                for rows in self.db.rows.values():
                    for row in rows:
                        row.setdefault("created_at", "2026-01-01T00:00:00Z")
                        row.setdefault("updated_at", "2026-01-01T00:00:00Z")
        return None

    def provider(self, request):
        if request.url.host not in HOSTS:
            raise AssertionError("Unexpected provider destination in synthetic fixture")
        provider = HOSTS[request.url.host]
        body = json.loads(request.content)
        classifier = provider == "cerebras" and any("routing classifier for Philomath" in m.get("content", "") for m in body.get("messages", []))
        with self.guard:
            self.provider_calls.append({"provider": provider, "body": body, "classifier": classifier})
            failures = self.provider_failures.get(provider, [])
            status = failures.pop(0) if failures else 200
        if self.provider_delay:
            time.sleep(self.provider_delay)
        if status != 200:
            return httpx.Response(status, json={"error": {"message": "Synthetic provider unavailable", "type": "server_error"}})
        if provider == "gemini":
            return httpx.Response(200, json={"candidates": [{"content": {
                "role": "model", "parts": [{"text": ANSWER}]}, "finishReason": "STOP"}]})
        return httpx.Response(200, json={"id": "synthetic-completion", "object": "chat.completion",
            "created": 1, "model": body["model"], "choices": [{"index": 0,
                "message": {"role": "assistant", "content": "coding" if classifier else ANSWER}, "finish_reason": "stop"}]})

    def async_client(self):
        owned = httpx.AsyncClient(transport=httpx.MockTransport(self.db.handle), cookies=stateless_cookies())
        self.async_clients.append(owned)
        return owned

    def __enter__(self):
        try:
            self.stack.enter_context(patch.dict(os.environ, {
                "PYTHON_DOTENV_DISABLED": "1", "PHILOMATH_ENCRYPTION_KEY": Fernet.generate_key().decode(),
            }))
            self.db = SyntheticDatabase()
            self.db.hook = self.database_hook
            self.stack.callback(self.db.http.close)
            self.controller = request_limits.AdmissionController(clock=lambda: time.monotonic() + self.now)
            old_stack = app.middleware_stack
            app.middleware_stack = None
            self.stack.callback(setattr, app, "middleware_stack", old_stack)
            for name, value in (("supabase", self.db.client), ("_http_client", self.db.http)):
                self.stack.enter_context(patch.object(supabase_service, name, value))
            for module in (supabase_service, startup_service):
                self.stack.enter_context(patch.object(module, "_configuration", return_value=(
                    "https://synthetic-database.invalid", "synthetic-service-key")))
            self.stack.enter_context(patch.object(startup_service, "_client", None))
            self.stack.enter_context(patch.object(startup_service, "_new_http_client", side_effect=self.async_client))
            self.stack.enter_context(patch.object(request_limits, "AdmissionController", return_value=self.controller))
            self.stack.enter_context(patch.object(clients, "_get_transport", return_value=httpx.MockTransport(self.provider)))
            self.http = self.stack.enter_context(TestClient(app))
            return self
        except BaseException:
            self.stack.close()
            raise

    def __exit__(self, *_):
        self.stack.close()

    def headers(self, user="a"):
        return {"Authorization": "Bearer synthetic-user-" + user}

    def request(self, method, path, user="a", **kwargs):
        return self.http.request(method, path, headers=self.headers(user), **kwargs)

    def add_provider(self, provider, user="a"):
        response = self.request("POST", "/api/integrations", user,
            json={"ai_name": provider, "api_key": "synthetic-byok-key-" + provider + "-" + user})
        if response.status_code != 200:
            raise AssertionError("Synthetic integration setup failed: " + str(response.status_code))
        return response

    def send(self, mode="basic", provider=None, conversation="synthetic-chat", user="a", **kwargs):
        body = {"message": "Explain how this example works.", "mode": mode,
                "conversation_id": conversation, **kwargs}
        if provider:
            body["provider"] = provider
        return self.request("POST", "/api/chat/send", user, json=body)
