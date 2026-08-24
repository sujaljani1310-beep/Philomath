from dataclasses import dataclass

from fastapi import Header, HTTPException, status

from app.services.supabase_service import require_supabase


@dataclass(frozen=True)
class AuthenticatedUser:
    id: str
    email: str | None = None


def _extract_bearer_token(authorization: str | None) -> str:
    if not authorization:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Sign in with Google to use Philomath.",
        )

    scheme, _, token = authorization.partition(" ")

    if scheme.lower() != "bearer" or not token.strip():
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid authorization header.",
        )

    return token.strip()


def get_current_user(
    authorization: str | None = Header(default=None),
) -> AuthenticatedUser:
    token = _extract_bearer_token(authorization)
    client = require_supabase()

    try:
        response = client.auth.get_user(token)
        user = response.user
    except Exception as error:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Your login session is invalid or expired. Please sign in again.",
        ) from error

    if not user or not user.id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Could not identify the signed-in user.",
        )

    return AuthenticatedUser(
        id=str(user.id),
        email=getattr(user, "email", None),
    )
