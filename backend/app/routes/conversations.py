from fastapi import APIRouter, Depends

from app.services.auth_service import AuthenticatedUser, get_current_user
from app.services.startup_service import get_startup_user, list_startup_conversations
from app.services.persistence import PersistenceUnavailable
from app.services.conversation_service import (
    delete_conversation,
    get_conversation_messages,
    is_supabase_connected,
)

router = APIRouter()


@router.get("/api/conversations")
async def get_conversations(
    user: AuthenticatedUser = Depends(get_startup_user),
):
    return {"conversations": await list_startup_conversations(user.id)}


@router.get("/api/conversations/{conversation_id}")
def get_conversation(
    conversation_id: str,
    user: AuthenticatedUser = Depends(get_current_user),
):
    if not is_supabase_connected():
        return {
            "error": "Supabase is not connected. Check the backend environment."
        }

    return {
        "conversation_id": conversation_id,
        "messages": get_conversation_messages(conversation_id, user.id),
    }


@router.delete("/api/conversations/{conversation_id}")
def remove_conversation(
    conversation_id: str,
    user: AuthenticatedUser = Depends(get_current_user),
):
    if not is_supabase_connected():
        raise PersistenceUnavailable()

    delete_conversation(conversation_id, user.id)

    return {
        "success": True,
        "conversation_id": conversation_id,
        "message": "Conversation deleted successfully.",
    }
