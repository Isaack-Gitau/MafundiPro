import hmac
import math
from datetime import UTC, datetime

from fastapi import APIRouter, Request, Response, status

from app.api.dependencies.database import DatabaseSession
from app.core.config import settings
from app.core.csrf import (
    InvalidOriginError,
    generate_csrf_token,
    validate_origin,
    verify_csrf_token,
)
from app.core.errors import AppError
from app.schemas.auth import (
    LoginCreate,
    LoginRead,
    RegistrationCreate,
    RegistrationRead,
)
from app.services.authentication import authenticate_user
from app.services.login_rate_limits import (
    LoginRateLimitExceeded,
    check_login_rate_limit,
    record_failed_login,
    reset_username_ip_rate_limit,
)
from app.services.registration import RegistrationConflictError, register_user
from app.services.sessions import (
    SessionNotEligibleError,
    create_session,
    revoke_session,
)

router = APIRouter()


def _validate_origin(request: Request) -> None:
    """Reject requests originating from an untrusted browser origin."""
    try:
        validate_origin(request.headers.get("origin"))
    except InvalidOriginError as exc:
        raise AppError(
            status_code=status.HTTP_403_FORBIDDEN,
            code="UNTRUSTED_ORIGIN",
            message="The request origin is not allowed.",
        ) from exc


def _client_ip(request: Request) -> str:
    """Return the directly connected client IP without trusting forwarded headers."""
    if request.client is None:
        raise AppError(
            status_code=status.HTTP_400_BAD_REQUEST,
            code="CLIENT_ADDRESS_UNAVAILABLE",
            message="The client address could not be determined.",
        )

    return request.client.host


def _set_authentication_cookies(
    response: Response,
    session_token: str,
    csrf_token: str,
    absolute_expires_at: datetime,
) -> None:
    """Set the server-managed session cookie and readable CSRF cookie."""
    now = datetime.now(UTC)
    max_age = max(1, math.ceil((absolute_expires_at - now).total_seconds()))

    cookie_options = {
        "secure": settings.session_cookie_secure,
        "samesite": settings.session_cookie_same_site,
        "path": "/",
        "max_age": max_age,
    }

    response.set_cookie(
        key=settings.session_cookie_name,
        value=session_token,
        httponly=True,
        **cookie_options,
    )

    response.set_cookie(
        key=settings.csrf_cookie_name,
        value=csrf_token,
        httponly=False,
        **cookie_options,
    )


def _clear_authentication_cookies(response: Response) -> None:
    """Clear authentication cookies using their original cookie attributes."""
    response.delete_cookie(
        key=settings.session_cookie_name,
        path="/",
        secure=settings.session_cookie_secure,
        httponly=True,
        samesite=settings.session_cookie_same_site,
    )

    response.delete_cookie(
        key=settings.csrf_cookie_name,
        path="/",
        secure=settings.session_cookie_secure,
        httponly=False,
        samesite=settings.session_cookie_same_site,
    )


@router.post(
    "/register",
    response_model=RegistrationRead,
    status_code=status.HTTP_201_CREATED,
    summary="Register a new account",
)
def register(
    registration: RegistrationCreate,
    db: DatabaseSession,
) -> RegistrationRead:
    """Register an account that must complete phone verification."""
    try:
        user = register_user(db, registration)
        db.commit()
        db.refresh(user)
    except RegistrationConflictError as exc:
        db.rollback()
        raise AppError(
            status_code=status.HTTP_409_CONFLICT,
            code="REGISTRATION_CONFLICT",
            message="An account with these details already exists.",
        ) from exc

    return RegistrationRead.model_validate(user)


@router.post(
    "/login",
    response_model=LoginRead,
    summary="Authenticate an account",
)
def login(
    credentials: LoginCreate,
    request: Request,
    response: Response,
    db: DatabaseSession,
) -> LoginRead:
    """Authenticate an eligible account and establish a server-managed session."""
    _validate_origin(request)
    client_ip = _client_ip(request)

    try:
        check_login_rate_limit(
            db,
            credentials.username,
            client_ip,
        )
    except LoginRateLimitExceeded as exc:
        raise AppError(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            code="LOGIN_RATE_LIMITED",
            message="Too many login attempts. Please try again later.",
            headers={"Retry-After": str(exc.retry_after_seconds)},
        ) from exc

    user = authenticate_user(
        db,
        credentials.username,
        credentials.password,
    )

    if user is None:
        record_failed_login(
            db,
            credentials.username,
            client_ip,
        )
        db.commit()

        raise AppError(
            status_code=status.HTTP_401_UNAUTHORIZED,
            code="INVALID_CREDENTIALS",
            message=(
                "The username or password is incorrect, or the account is not eligible to sign in."
            ),
        )

    try:
        session_token, user_session = create_session(db, user.id)
    except SessionNotEligibleError:
        db.rollback()

        record_failed_login(
            db,
            credentials.username,
            client_ip,
        )
        db.commit()

        raise AppError(
            status_code=status.HTTP_401_UNAUTHORIZED,
            code="INVALID_CREDENTIALS",
            message=(
                "The username or password is incorrect, or the account is not eligible to sign in."
            ),
        ) from None

    reset_username_ip_rate_limit(
        db,
        credentials.username,
        client_ip,
    )

    db.commit()

    csrf_token = generate_csrf_token(session_token)

    _set_authentication_cookies(
        response,
        session_token,
        csrf_token,
        user_session.absolute_expires_at,
    )

    return LoginRead(
        id=user.id,
        full_name=user.full_name,
        username=user.username,
        csrf_token=csrf_token,
    )


@router.post(
    "/logout",
    summary="End the current session",
)
def logout(
    request: Request,
    response: Response,
    db: DatabaseSession,
) -> dict[str, bool]:
    """Revoke the current session and clear its authentication cookies."""
    _validate_origin(request)

    session_token = request.cookies.get(settings.session_cookie_name)

    if not session_token:
        _clear_authentication_cookies(response)
        return {"logged_out": False}

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

    logged_out = revoke_session(db, session_token)
    db.commit()

    _clear_authentication_cookies(response)

    return {"logged_out": logged_out}
