from fastapi import APIRouter, Depends, HTTPException, status

from app.models.schemas import AddAIRequest
from app.services.auth_service import AuthenticatedUser, get_current_user
from app.services.integration_service import (
    delete_integration,
    list_integrations,
    save_integration,
)

router = APIRouter()


@router.get("/api/integrations")
def get_integrations(
    user: AuthenticatedUser = Depends(get_current_user),
):
    return {
        "integrations": list_integrations(user.id),
    }


@router.post("/api/integrations")
def add_integration(
    request: AddAIRequest,
    user: AuthenticatedUser = Depends(get_current_user),
):
    try:
        integration = save_integration(
            user_id=user.id,
            ai_name=request.ai_name,
            api_key=request.api_key,
        )
    except ValueError as error:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(error),
        ) from error

    return {
        "success": True,
        "integration": integration,
    }


@router.delete("/api/integrations/{provider}")
def remove_integration(
    provider: str,
    user: AuthenticatedUser = Depends(get_current_user),
):
    try:
        delete_integration(user.id, provider)
    except ValueError as error:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(error),
        ) from error

    return {
        "success": True,
        "provider": provider,
    }
