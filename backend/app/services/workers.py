from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.identity import User
from app.models.worker import WorkerProfile


class WorkerProfileError(Exception):
    """Base exception for worker profile operations."""


class WorkerProfileNotFoundError(WorkerProfileError):
    """Raised when the authenticated user has no worker profile."""


class WorkerProfileConflictError(WorkerProfileError):
    """Raised when a worker profile already exists or cannot be created."""


def create_worker_profile(db: Session, user_id: UUID) -> WorkerProfile:
    """Create one worker profile for an eligible authenticated account."""
    user = db.scalar(
        select(User)
        .where(User.id == user_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )

    if user is None or user.account_status != "active" or user.phone_verified_at is None:
        raise WorkerProfileNotFoundError("The eligible account was not found.")

    existing_profile = db.scalar(select(WorkerProfile.id).where(WorkerProfile.user_id == user_id))

    if existing_profile is not None:
        raise WorkerProfileConflictError("A worker profile already exists.")

    profile = WorkerProfile(
        user_id=user.id,
        display_name=user.full_name,
        availability="unavailable",
    )

    try:
        with db.begin_nested():
            db.add(profile)
            db.flush()
    except IntegrityError as exc:
        diagnostic = getattr(exc.orig, "diag", None)
        constraint_name = getattr(diagnostic, "constraint_name", None)

        if constraint_name == "uq_worker_profiles_user_id":
            raise WorkerProfileConflictError("A worker profile already exists.") from exc

        raise

    return profile


def get_worker_profile(db: Session, user_id: UUID) -> WorkerProfile:
    """Retrieve the worker profile belonging to the authenticated account."""
    profile = db.scalar(select(WorkerProfile).where(WorkerProfile.user_id == user_id))

    if profile is None:
        raise WorkerProfileNotFoundError("Worker profile was not found.")

    return profile


def update_worker_profile(
    db: Session,
    user_id: UUID,
    updates: dict[str, object],
) -> WorkerProfile:
    """Update only the supplied fields on the authenticated user's profile."""
    profile = get_worker_profile(db, user_id)

    for field, value in updates.items():
        setattr(profile, field, value)

    db.flush()

    return profile
