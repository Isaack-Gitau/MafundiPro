import secrets

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.security import hash_password, password_needs_rehash, verify_password
from app.models.identity import User

_DUMMY_PASSWORD_HASH = hash_password(secrets.token_urlsafe(32))


def authenticate_user(
    db: Session,
    username: str,
    password: str,
) -> User | None:
    """Authenticate an active, phone-verified user without exposing account state."""
    username_key = username.strip().casefold()

    user = db.scalar(select(User).where(User.username_key == username_key))

    password_hash = user.password_hash if user is not None else _DUMMY_PASSWORD_HASH

    password_is_valid = verify_password(password, password_hash)

    if (
        user is None
        or not password_is_valid
        or user.account_status != "active"
        or user.phone_verified_at is None
    ):
        return None

    if password_needs_rehash(user.password_hash):
        user.password_hash = hash_password(password)
        db.flush()

    return user
