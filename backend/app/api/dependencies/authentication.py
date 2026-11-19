import hmac
from typing import Annotated

from fastapi import Depends, Request, status

from app.api.dependencies.database import AuthDatabaseSession, DatabaseSession
from app.core.config import settings
from app.core.csrf import InvalidOriginError, validate_origin, verify_csrf_token
from app.core.errors import AppError
from app.models.identity import User
from app.services.sessions import get_valid_session, revoke_session

_UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


def _validate_request_origin(request: Request) -> None:
    """Validate the request Origin header when one is supplied."""
    try:
        validate_origin(request.headers.get("origin"))
    except InvalidOriginError as exc:
        raise AppError(
            status_code=status.HTTP_403_FORBIDDEN,
            code="UNTRUSTED_ORIGIN",
            message="The request origin is not allowed.",
        ) from exc


def _validate_csrf(request: Request, session_token: str) -> None:
    """Validate the CSRF header, cookie pairing, and session-bound signature."""
    csrf_header = request.headers.get("x-csrf-token")
    csrf_cookie = request.cookies.get(settings.csrf_cookie_name)

    if (
        not csrf_header
        or (csrf_cookie is not None and not hmac.compare_digest(csrf_cookie, csrf_header))
        or not verify_csrf_token(session_token, csrf_header)
    ):
        raise AppError(
            status_code=status.HTTP_403_FORBIDDEN,
            code="INVALID_CSRF_TOKEN",
            message="The request could not be verified.",
        )


def get_current_user(
    db: DatabaseSession,
    auth_db: AuthDatabaseSession,
    request: Request,
) -> User:
    """Resolve the current user using an independent authentication transaction."""
    _validate_request_origin(request)

    session_token = request.cookies.get(settings.session_cookie_name)

    if not session_token:
        raise AppError(
            status_code=status.HTTP_401_UNAUTHORIZED,
            code="AUTHENTICATION_REQUIRED",
            message="Authentication is required.",
        )

    if request.method.upper() in _UNSAFE_METHODS:
        _validate_csrf(request, session_token)

    user_session = get_valid_session(auth_db, session_token)

    if user_session is None:
        auth_db.commit()
        raise AppError(
            status_code=status.HTTP_401_UNAUTHORIZED,
            code="INVALID_SESSION",
            message="Your session is invalid or has expired.",
        )

    user_id = user_session.user_id

    # Persist session refreshes independently of endpoint transactions.
    auth_db.commit()

    # Keep the returned ORM object attached to the endpoint's database session.
    user = db.get(User, user_id)

    if user is None or user.account_status != "active" or user.phone_verified_at is None:
        revoke_session(auth_db, session_token)
        auth_db.commit()
        raise AppError(
            status_code=status.HTTP_401_UNAUTHORIZED,
            code="INVALID_SESSION",
            message="Your session is invalid or has expired.",
        )

    return user


CurrentUser = Annotated[User, Depends(get_current_user)]
