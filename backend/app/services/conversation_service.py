import os
from datetime import datetime
from typing import List, Optional

from dotenv import load_dotenv
from supabase import Client, create_client

load_dotenv()

supabase_url = os.getenv("SUPABASE_URL")
supabase_key = os.getenv("SUPABASE_KEY")

if supabase_url and supabase_key:
    supabase: Client = create_client(supabase_url, supabase_key)
else:
    supabase = None


def is_supabase_connected() -> bool:
    return supabase is not None


def ensure_conversation_exists(
    conversation_id: str,
    mode: str,
    title: Optional[str] = None,
):
    existing = (
        supabase
        .table("conversations")
        .select("id")
        .eq("id", conversation_id)
        .execute()
    )

    if existing.data:
        supabase.table("conversations").update({
            "updated_at": datetime.now().isoformat()
        }).eq("id", conversation_id).execute()
        return

    conversation_title = (title or "New Chat").strip() or "New Chat"

    supabase.table("conversations").insert({
        "id": conversation_id,
        "title": conversation_title,
        "mode": mode,
    }).execute()


def save_message(
    conversation_id: str,
    role: str,
    content: str,
    provider_used: Optional[str] = None,
    model_used: Optional[str] = None,
):
    supabase.table("messages").insert({
        "conversation_id": conversation_id,
        "role": role,
        "content": content,
        "provider_used": provider_used,
        "model_used": model_used,
    }).execute()


def load_recent_messages(conversation_id: str, limit: int = 10) -> List[dict]:
    result = (
        supabase
        .table("messages")
        .select("role, content")
        .eq("conversation_id", conversation_id)
        .order("created_at", desc=True)
        .limit(limit)
        .execute()
    )

    messages = result.data or []
    messages.reverse()
    return messages


def count_messages(conversation_id: str) -> int:
    result = (
        supabase
        .table("messages")
        .select("id", count="exact")
        .eq("conversation_id", conversation_id)
        .execute()
    )
    return result.count or 0


def get_all_conversations():
    result = (
        supabase
        .table("conversations")
        .select("*")
        .order("updated_at", desc=True)
        .execute()
    )
    return result.data or []


def get_conversation_messages(conversation_id: str):
    result = (
        supabase
        .table("messages")
        .select("*")
        .eq("conversation_id", conversation_id)
        .order("created_at")
        .execute()
    )
    return result.data or []


def delete_conversation(conversation_id: str):
    supabase.table("messages").delete().eq(
        "conversation_id",
        conversation_id,
    ).execute()

    result = (
        supabase
        .table("conversations")
        .delete()
        .eq("id", conversation_id)
        .execute()
    )
    return result.data
