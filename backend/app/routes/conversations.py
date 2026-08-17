from fastapi import APIRouter

from app.services.conversation_service import (
    delete_conversation,
    get_all_conversations,
    get_conversation_messages,
    is_supabase_connected,
)

router = APIRouter()


@router.get("/api/conversations")
def get_conversations():
    if not is_supabase_connected():
        return {
            "error": "Supabase is not connected. Check SUPABASE_URL and SUPABASE_KEY in .env."
        }

    return {"conversations": get_all_conversations()}


@router.get("/api/conversations/{conversation_id}")
def get_conversation(conversation_id: str):
    if not is_supabase_connected():
        return {
            "error": "Supabase is not connected. Check SUPABASE_URL and SUPABASE_KEY in .env."
        }

    return {
        "conversation_id": conversation_id,
        "messages": get_conversation_messages(conversation_id),
    }


@router.delete("/api/conversations/{conversation_id}")
def remove_conversation(conversation_id: str):
    if not is_supabase_connected():
        return {
            "success": False,
            "error": "Supabase is not connected. Check SUPABASE_URL and SUPABASE_KEY in .env.",
        }

    delete_conversation(conversation_id)

    return {
        "success": True,
        "conversation_id": conversation_id,
        "message": "Conversation deleted successfully.",
    }
