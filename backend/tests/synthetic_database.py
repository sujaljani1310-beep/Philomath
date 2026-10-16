"""Small atomic in-memory PostgREST peer; all requests use real installed SDKs."""

import json
from threading import Lock

import httpx
from supabase import ClientOptions, create_client


class SyntheticDatabase:
    def __init__(self):
        self.lock = Lock()
        self.rows = {"conversations": [], "messages": [], "user_ai_integrations": []}
        self.requests = []
        self.hook = lambda stage, request, body: None
        self.sequence = 0
        self.http = httpx.Client(transport=httpx.MockTransport(self.handle))
        self.client = create_client(
            "https://synthetic-database.invalid", "synthetic-service-key",
            options=ClientOptions(httpx_client=self.http),
        )
        self.client.postgrest  # Same eager-without-I/O initialization as production.

    def seed_conversation(self, owner="owner", conversation="chat"):
        self.rows["conversations"].append({"id": conversation, "user_id": owner, "title": "Synthetic", "mode": "basic"})

    def _hook(self, stage, request, body):
        result = self.hook(stage, request, body)
        if isinstance(result, BaseException):
            raise result
        return result

    def handle(self, request):
        table = request.url.path.rsplit("/", 1)[-1]
        body = json.loads(request.content) if request.content else None
        with self.lock:
            self.requests.append(request)
        override = self._hook("before", request, body)
        if override is not None:
            return override
        with self.lock:
            def matches(row):
                return all(
                    str(row.get(name)) == value[3:]
                    for name, value in request.url.params.multi_items() if value.startswith("eq.")
                )
            selected = [row for row in self.rows[table] if matches(row)]
            if request.method in {"GET", "HEAD"}:
                changed = selected
            elif request.method == "POST":
                if table == "conversations" and any(row["id"] == body["id"] for row in self.rows[table]):
                    return httpx.Response(409, json={"code": "23505", "message": "synthetic duplicate", "details": None, "hint": None})
                if table == "messages" and not any(row["id"] == body["conversation_id"] for row in self.rows["conversations"]):
                    return httpx.Response(409, json={"code": "23503", "message": "synthetic foreign key", "details": None, "hint": None})
                conflict = None
                if table == "user_ai_integrations":
                    if request.url.params.get("on_conflict") != "user_id,provider":
                        raise AssertionError("Integration write must use the existing owner/provider conflict key")
                    conflict = next((row for row in self.rows[table] if (row["user_id"], row["provider"]) == (body["user_id"], body["provider"])), None)
                if conflict is not None:
                    conflict.update(body)
                    changed = [conflict]
                else:
                    self.sequence += 1
                    row = {"id": f"synthetic-{self.sequence}", **body}
                    self.rows[table].append(row)
                    changed = [row]
            elif request.method == "PATCH":
                for row in selected:
                    row.update(body)
                changed = selected
            elif request.method == "DELETE":
                self.rows[table] = [row for row in self.rows[table] if not matches(row)]
                changed = selected
            else:
                raise AssertionError("Unexpected database method")
            count = len(changed)
            columns = request.url.params.get("select", "*")
            data = [dict(row) if columns == "*" else {key.strip(): row.get(key.strip()) for key in columns.split(",")} for row in changed]
        override = self._hook("after", request, body)
        if override is not None:
            return override
        headers = {}
        if "count=exact" in request.headers.get("prefer", ""):
            headers["content-range"] = f"*/{count}"
        if request.method == "HEAD":
            return httpx.Response(200, headers=headers)
        if request.method not in {"GET", "HEAD"} and "return=minimal" in request.headers.get("prefer", ""):
            return httpx.Response(201 if request.method == "POST" else 204, headers=headers)
        return httpx.Response(201 if request.method == "POST" else 200, json=data, headers=headers)
