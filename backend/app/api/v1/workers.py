from fastapi import APIRouter, status

from app.api.dependencies.authentication import CurrentUser
from app.api.dependencies.database import DatabaseSession
from app.core.errors import AppError
from app.models.worker import WorkerProfile
from app.schemas.workers import WorkerProfileRead
from app.services.workers import (
    WorkerProfileConflictError,
    WorkerProfileNotFoundError,
    create_worker_profile,
    get_worker_profile,
)

router = APIRouter()


@router.post(
    "/me",
    response_model=WorkerProfileRead,
    status_code=status.HTTP_201_CREATED,
    summary="Create the authenticated user's worker profile",
)
def create_me_worker_profile(
    current_user: CurrentUser,
    db: DatabaseSession,
) -> WorkerProfileRead:
    """Create a worker profile for the authenticated account."""
    try:
        profile = create_worker_profile(db, current_user.id)
        db.commit()
        db.refresh(profile)
    except WorkerProfileConflictError as exc:
        db.rollback()
        raise AppError(
            status_code=409,
            code="WORKER_PROFILE_EXISTS",
            message=str(exc),
        ) from exc
    except WorkerProfileNotFoundError as exc:
        db.rollback()
        raise AppError(
            status_code=404,
            code="WORKER_PROFILE_ACCOUNT_NOT_FOUND",
            message=str(exc),
        ) from exc

    return WorkerProfileRead.model_validate(profile)


@router.get(
    "/me",
    response_model=WorkerProfileRead,
    summary="Get the authenticated user's worker profile",
)
def get_me_worker_profile(
    current_user: CurrentUser,
    db: DatabaseSession,
) -> WorkerProfileRead:
    """Retrieve the worker profile belonging to the authenticated account."""
    try:
        profile: WorkerProfile = get_worker_profile(db, current_user.id)
    except WorkerProfileNotFoundError as exc:
        raise AppError(
            status_code=404,
            code="WORKER_PROFILE_NOT_FOUND",
            message=str(exc),
        ) from exc

    return WorkerProfileRead.model_validate(profile)
