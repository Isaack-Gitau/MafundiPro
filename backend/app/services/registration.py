from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.security import hash_password
from app.models.identity import User
from app.schemas.auth import RegistrationCreate


class RegistrationError(Exception):
    """Base exception for registration errors."""


class RegistrationConflictError(RegistrationError):
    """Raised when registration details conflict with an existing account."""


def register_user(
    db: Session,
    registration: RegistrationCreate,
) -> User:
    """Create an unverified account without creating an authenticated session."""
    username_key = registration.username.casefold()

    existing_user = db.scalar(
        select(User.id).where(
            or_(
                User.username_key == username_key,
                User.phone_number == registration.phone_number,
            )
        )
    )

    if existing_user is not None:
        raise RegistrationConflictError("An account with these details already exists.")

    user = User(
        full_name=registration.full_name,
        username=registration.username,
        username_key=username_key,
        phone_number=registration.phone_number,
        password_hash=hash_password(registration.password),
        account_status="pending_verification",
    )

    try:
        with db.begin_nested():
            db.add(user)
            db.flush()
    except IntegrityError as exc:
        diagnostic = getattr(exc.orig, "diag", None)
        constraint_name = getattr(diagnostic, "constraint_name", None)

        if constraint_name in {
            "uq_users_username_key",
            "uq_users_phone_number",
        }:
            raise RegistrationConflictError(
                "An account with these details already exists."
            ) from exc

        raise

    return user
