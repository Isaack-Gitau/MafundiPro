from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.employer import EmployerProfile
from app.models.identity import User


class EmployerProfileError(Exception):
    """Base exception for employer profile operations."""


class EmployerProfileNotFoundError(EmployerProfileError):
    """Raised when an eligible account or employer profile cannot be found."""


class EmployerProfileConflictError(EmployerProfileError):
    """Raised when an employer profile already exists or cannot be created."""


def create_employer_profile(
    db: Session,
    user_id: UUID,
    employer_type: str,
    description: str | None = None,
) -> EmployerProfile:
    """Create one employer profile for an eligible authenticated account."""
    user = db.scalar(
        select(User)
        .where(User.id == user_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )

    if user is None or user.account_status != "active" or user.phone_verified_at is None:
        raise EmployerProfileNotFoundError("The eligible account was not found.")

    existing_profile = db.scalar(
        select(EmployerProfile.id).where(EmployerProfile.user_id == user_id)
    )

    if existing_profile is not None:
        raise EmployerProfileConflictError("An employer profile already exists.")

    first_name = user.full_name.strip().split()[0]

    profile = EmployerProfile(
        user_id=user.id,
        employer_type=employer_type,
        display_name=f"@{first_name}",
        description=description,
    )

    try:
        with db.begin_nested():
            db.add(profile)
            db.flush()
    except IntegrityError as exc:
        diagnostic = getattr(exc.orig, "diag", None)
        constraint_name = getattr(diagnostic, "constraint_name", None)

        if constraint_name == "uq_employer_profiles_user_id":
            raise EmployerProfileConflictError("An employer profile already exists.") from exc

        raise

    return profile


def get_employer_profile(db: Session, user_id: UUID) -> EmployerProfile:
    """Retrieve the employer profile belonging to the authenticated account."""
    profile = db.scalar(select(EmployerProfile).where(EmployerProfile.user_id == user_id))

    if profile is None:
        raise EmployerProfileNotFoundError("Employer profile was not found.")

    return profile


def update_employer_profile(
    db: Session,
    user_id: UUID,
    updates: dict[str, object],
) -> EmployerProfile:
    """Update only supplied fields on the authenticated user's employer profile."""
    profile = db.scalar(
        select(EmployerProfile)
        .where(EmployerProfile.user_id == user_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )

    if profile is None:
        raise EmployerProfileNotFoundError("Employer profile was not found.")

    for field, value in updates.items():
        setattr(profile, field, value)

    db.flush()

    return profile
