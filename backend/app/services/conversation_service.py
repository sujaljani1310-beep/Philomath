from datetime import datetime
from contextlib import contextmanager
from contextvars import ContextVar
from typing import List, Optional

from app.observability import timed, timed_operation
from app.services.persistence import (
    PersistenceUnavailable, affected_rows, database_operation, mutation_guard,
)
from app.services.supabase_service import is_supabase_connected, require_supabase


_history_snapshot: ContextVar[dict | None] = ContextVar("history_snapshot", default=None)


@contextmanager
def conversation_history_snapshot():
    """Keep fallbacks on one successful history read, only for this chat request."""
    token = _history_snapshot.set({})
    try:
        yield
    finally:
        _history_snapshot.reset(token)


def _invalidate_history(conversation_id: str, user_id: str) -> None:
    snapshot = _history_snapshot.get()
    if snapshot is not None:
        for key in list(snapshot):
            if key[:2] == (user_id, conversation_id):
                del snapshot[key]


def _owned_conversation_exists(result, conversation_id: str) -> bool:
    rows = result.data
    if rows == []:
        return False
    if (
        not isinstance(rows, list) or len(rows) != 1
        or not isinstance(rows[0], dict) or rows[0].get("id") != conversation_id
    ):
        raise PersistenceUnavailable()
    return True


@timed("db.conversation.ensure")
def ensure_conversation_exists(
    conversation_id: str,
    mode: str,
    user_id: str,
    title: Optional[str] = None,
):
    with mutation_guard("conversation", (user_id, conversation_id)), database_operation():
        client = require_supabase()
        existing = (
            client.table("conversations").select("id")
            .eq("id", conversation_id).eq("user_id", user_id).limit(1).execute()
        )
        if _owned_conversation_exists(existing, conversation_id):
            result = (
                client.table("conversations")
                .update({"updated_at": datetime.now().isoformat()}, returning="minimal", count="exact")
                .eq("id", conversation_id).eq("user_id", user_id)
                .retry(False).execute()
            )
        else:
            # Never upsert on a globally unique conversation ID: a conflict
            # might belong to another user. Do not retry even a failed insert.
            result = (
                client.table("conversations").insert({
                    "id": conversation_id,
                    "title": (title or "New Chat").strip() or "New Chat",
                    "mode": mode,
                    "user_id": user_id,
                }, returning="minimal", count="exact").retry(False).execute()
            )
        affected_rows(result, expected=1)


@timed("db.message.save")
def save_message(
    conversation_id: str,
    user_id: str,
    role: str,
    content: str,
    provider_used: Optional[str] = None,
    model_used: Optional[str] = None,
):
    with mutation_guard("conversation", (user_id, conversation_id)), database_operation():
        client = require_supabase()
        owned_conversation = (
            client.table("conversations").select("id")
            .eq("id", conversation_id).eq("user_id", user_id).limit(1).execute()
        )
        if not _owned_conversation_exists(owned_conversation, conversation_id):
            raise PersistenceUnavailable()
        try:
            result = (
                client.table("messages").insert({
                    "conversation_id": conversation_id,
                    "user_id": user_id,
                    "role": role,
                    "content": content,
                    "provider_used": provider_used,
                    "model_used": model_used,
                }, returning="minimal", count="exact").retry(False).execute()
            )
            affected_rows(result, expected=1)
        finally:
            # A write may have committed even when its response was lost.
            _invalidate_history(conversation_id, user_id)


def load_recent_messages(
    conversation_id: str,
    user_id: str,
    limit: int = 10,
) -> List[dict]:
    snapshot = _history_snapshot.get()
    key = (user_id, conversation_id, limit)
    if snapshot is not None and key in snapshot:
        return [dict(message) for message in snapshot[key]]
    with database_operation(), timed_operation("db.messages.recent"):
        client = require_supabase()
        result = (
            client
            .table("messages")
            .select("role, content")
            .eq("conversation_id", conversation_id)
            .eq("user_id", user_id)
            .order("created_at", desc=True)
            .limit(limit)
            .execute()
        )

        rows = result.data
        if not isinstance(rows, list) or any(
            not isinstance(row, dict)
            or not isinstance(row.get("role"), str)
            or not isinstance(row.get("content"), str)
            for row in rows
        ):
            raise PersistenceUnavailable()
        messages = [dict(row) for row in reversed(rows)]
        if snapshot is not None:
            snapshot[key] = [dict(message) for message in messages]
        return messages


@timed("db.messages.count")
def count_messages(conversation_id: str, user_id: str) -> int:
    with database_operation():
        client = require_supabase()
        result = (
            client.table("messages").select("id", count="exact", head=True)
            .eq("conversation_id", conversation_id).eq("user_id", user_id)
            .retry(False).execute()
        )
        return affected_rows(result)


@timed("db.conversations.list")
def get_all_conversations(user_id: str):
    client = require_supabase()

    result = (
        client
        .table("conversations")
        .select("*")
        .eq("user_id", user_id)
        .order("updated_at", desc=True)
        .execute()
    )
    return result.data or []


@timed("db.conversation.messages")
def get_conversation_messages(conversation_id: str, user_id: str):
    client = require_supabase()

    result = (
        client
        .table("messages")
        .select("*")
        .eq("conversation_id", conversation_id)
        .eq("user_id", user_id)
        .order("created_at")
        .execute()
    )
    return result.data or []


@timed("db.conversation.delete")
def delete_conversation(conversation_id: str, user_id: str):
    with mutation_guard("conversation", (user_id, conversation_id)), database_operation():
        client = require_supabase()
        try:
            result = (
                client.table("messages").delete(returning="minimal", count="exact")
                .eq("conversation_id", conversation_id).eq("user_id", user_id)
                .retry(False).execute()
            )
            affected_rows(result)
            result = (
                client.table("conversations").delete(returning="minimal", count="exact")
                .eq("id", conversation_id).eq("user_id", user_id)
                .retry(False).execute()
            )
            if affected_rows(result) > 1:
                raise PersistenceUnavailable()
            # Absence is the idempotent DELETE result. Confirm both resources,
            # since the existing two statements are not a transaction.
            result = (
                client.table("conversations").select("id", count="exact", head=True)
                .eq("id", conversation_id).eq("user_id", user_id)
                .retry(False).execute()
            )
            affected_rows(result, expected=0)
            if count_messages(conversation_id, user_id) != 0:
                raise PersistenceUnavailable()
        finally:
            _invalidate_history(conversation_id, user_id)
