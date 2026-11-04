from dataclasses import dataclass

from fastapi import Header, HTTPException, status

from app.observability import timed
from app.services.supabase_service import require_supabase


@dataclass(frozen=True)
class AuthenticatedUser:
    id: str
    email: str | None = None


_INVALID_TOKEN_CODES = frozenset({
    "bad_jwt", "jwt_expired", "no_authorization", "user_not_found", "user_banned",
})


def invalid_auth_response(status_code: int | None, code: str | None = None) -> bool:
    """Only definitive credential failures should ask the user to sign in again."""
    return status_code == 401 or (
        status_code in {400, 403, 422}
        and isinstance(code, str)
        and code in _INVALID_TOKEN_CODES
    )


def authentication_unavailable() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail="Authentication service is temporarily unavailable. Please try again.",
    )


def invalid_session() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Your login session is invalid or expired. Please sign in again.",
    )


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


@timed("auth.get_user")
def get_current_user(
    authorization: str | None = Header(default=None),
) -> AuthenticatedUser:
    token = _extract_bearer_token(authorization)
    try:
        client = require_supabase()
        response = client.auth.get_user(token)
        user = response.user
    except Exception as error:
        # Do not treat network errors, rate limits, or an unavailable auth
        # service as invalid credentials. Restrict status inspection to the SDK
        # exception type so unrelated exceptions cannot impersonate validation.
        from supabase_auth.errors import AuthApiError

        if isinstance(error, AuthApiError) and invalid_auth_response(
            error.status, error.code
        ):
            raise invalid_session() from error
        raise authentication_unavailable() from error

    if not user or not getattr(user, "id", None):
        raise authentication_unavailable()

    user = AuthenticatedUser(
        id=str(user.id),
        email=getattr(user, "email", None),
    )
    from app.request_limits import admit_authenticated_user

    admit_authenticated_user(user.id)
    return user
