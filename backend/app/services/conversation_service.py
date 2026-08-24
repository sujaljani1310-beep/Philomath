from datetime import datetime
from typing import List, Optional

from app.services.supabase_service import is_supabase_connected, require_supabase


def ensure_conversation_exists(
    conversation_id: str,
    mode: str,
    user_id: str,
    title: Optional[str] = None,
):
    client = require_supabase()

    existing = (
        client
        .table("conversations")
        .select("id")
        .eq("id", conversation_id)
        .eq("user_id", user_id)
        .execute()
    )

    if existing.data:
        (
            client
            .table("conversations")
            .update({"updated_at": datetime.now().isoformat()})
            .eq("id", conversation_id)
            .eq("user_id", user_id)
            .execute()
        )
        return

    conversation_title = (title or "New Chat").strip() or "New Chat"

    (
        client
        .table("conversations")
        .insert({
            "id": conversation_id,
            "title": conversation_title,
            "mode": mode,
            "user_id": user_id,
        })
        .execute()
    )


def save_message(
    conversation_id: str,
    user_id: str,
    role: str,
    content: str,
    provider_used: Optional[str] = None,
    model_used: Optional[str] = None,
):
    client = require_supabase()

    owned_conversation = (
        client
        .table("conversations")
        .select("id")
        .eq("id", conversation_id)
        .eq("user_id", user_id)
        .limit(1)
        .execute()
    )

    if not owned_conversation.data:
        raise ValueError("Conversation does not belong to the signed-in user.")

    (
        client
        .table("messages")
        .insert({
            "conversation_id": conversation_id,
            "user_id": user_id,
            "role": role,
            "content": content,
            "provider_used": provider_used,
            "model_used": model_used,
        })
        .execute()
    )


def load_recent_messages(
    conversation_id: str,
    user_id: str,
    limit: int = 10,
) -> List[dict]:
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

    messages = result.data or []
    messages.reverse()
    return messages


def count_messages(conversation_id: str, user_id: str) -> int:
    client = require_supabase()

    result = (
        client
        .table("messages")
        .select("id", count="exact")
        .eq("conversation_id", conversation_id)
        .eq("user_id", user_id)
        .execute()
    )
    return result.count or 0


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


def delete_conversation(conversation_id: str, user_id: str):
    client = require_supabase()

    (
        client
        .table("messages")
        .delete()
        .eq("conversation_id", conversation_id)
        .eq("user_id", user_id)
        .execute()
    )

    result = (
        client
        .table("conversations")
        .delete()
        .eq("id", conversation_id)
        .eq("user_id", user_id)
        .execute()
    )
    return result.data
