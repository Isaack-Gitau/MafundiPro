from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.security import generate_session_token, hash_session_token
from app.models.identity import User, UserSession

SESSION_REFRESH_INTERVAL = timedelta(minutes=1)


class SessionError(Exception):
    """Base exception for session lifecycle errors."""


class SessionNotEligibleError(SessionError):
    """Raised when an account is not eligible for a session."""


def _current_time(now: datetime | None = None) -> datetime:
    """Return a timezone-aware UTC timestamp."""
    value = now or datetime.now(UTC)

    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Session timestamps must be timezone-aware.")

    return value.astimezone(UTC)


def _lock_session_and_user(
    db: Session,
    token: str,
) -> tuple[UserSession | None, User | None]:
    """Lock and refresh the owning user before locking the session."""
    token_hash = hash_session_token(token)

    user_id = db.scalar(select(UserSession.user_id).where(UserSession.token_hash == token_hash))

    if user_id is None:
        return None, None

    user = db.scalar(
        select(User)
        .where(User.id == user_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )

    user_session = db.scalar(
        select(UserSession)
        .where(
            UserSession.token_hash == token_hash,
            UserSession.user_id == user_id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )

    return user_session, user


def create_session(
    db: Session,
    user_id: UUID,
    *,
    now: datetime | None = None,
) -> tuple[str, UserSession]:
    """Create a session for an active, phone-verified account."""
    current_time = _current_time(now)

    user = db.scalar(
        select(User)
        .where(User.id == user_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )

    if user is None or user.account_status != "active" or user.phone_verified_at is None:
        raise SessionNotEligibleError("This account is not eligible for authentication.")

    absolute_expires_at = current_time + timedelta(
        days=settings.session_absolute_timeout_days,
    )

    idle_expires_at = current_time + timedelta(
        minutes=settings.session_idle_timeout_minutes,
    )

    expires_at = min(idle_expires_at, absolute_expires_at)

    token = generate_session_token()

    user_session = UserSession(
        user_id=user.id,
        token_hash=hash_session_token(token),
        expires_at=expires_at,
        absolute_expires_at=absolute_expires_at,
        last_seen_at=current_time,
    )

    db.add(user_session)
    db.flush()

    return token, user_session


def get_valid_session(
    db: Session,
    token: str,
    *,
    now: datetime | None = None,
) -> UserSession | None:
    """Validate a session and periodically refresh its idle expiry."""
    current_time = _current_time(now)

    if not isinstance(token, str) or not token:
        return None

    user_session, user = _lock_session_and_user(db, token)

    if user_session is None or user_session.revoked_at is not None:
        return None

    if user_session.expires_at <= current_time or user_session.absolute_expires_at <= current_time:
        user_session.revoked_at = current_time
        db.flush()
        return None

    if user is None or user.account_status != "active" or user.phone_verified_at is None:
        user_session.revoked_at = current_time
        db.flush()
        return None

    if current_time - user_session.last_seen_at >= SESSION_REFRESH_INTERVAL:
        idle_expires_at = current_time + timedelta(
            minutes=settings.session_idle_timeout_minutes,
        )

        user_session.last_seen_at = current_time
        user_session.expires_at = min(
            idle_expires_at,
            user_session.absolute_expires_at,
        )

        db.flush()

    return user_session


def revoke_session(
    db: Session,
    token: str,
    *,
    now: datetime | None = None,
) -> bool:
    """Revoke a session. Repeated revocation is harmless."""
    current_time = _current_time(now)

    if not isinstance(token, str) or not token:
        return False

    user_session, _ = _lock_session_and_user(db, token)

    if user_session is None or user_session.revoked_at is not None:
        return False

    user_session.revoked_at = current_time
    db.flush()

    return True
