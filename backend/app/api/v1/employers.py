from fastapi import APIRouter, status

from app.api.dependencies.authentication import CurrentUser
from app.api.dependencies.database import DatabaseSession
from app.core.errors import AppError
from app.models.employer import EmployerProfile
from app.schemas.employers import (
    EmployerProfileCreate,
    EmployerProfileRead,
    EmployerProfileUpdate,
)
from app.services.employers import (
    EmployerProfileConflictError,
    EmployerProfileNotFoundError,
    create_employer_profile,
    get_employer_profile,
    update_employer_profile,
)

router = APIRouter()


@router.post(
    "/me",
    response_model=EmployerProfileRead,
    status_code=status.HTTP_201_CREATED,
    summary="Create the authenticated user's employer profile",
)
def create_me_employer_profile(
    payload: EmployerProfileCreate,
    current_user: CurrentUser,
    db: DatabaseSession,
) -> EmployerProfileRead:
    """Create an employer profile for the authenticated account."""
    try:
        profile = create_employer_profile(
            db,
            current_user.id,
            payload.employer_type,
            payload.description,
        )
        db.commit()
        db.refresh(profile)
    except EmployerProfileConflictError as exc:
        db.rollback()
        raise AppError(
            status_code=409,
            code="EMPLOYER_PROFILE_EXISTS",
            message=str(exc),
        ) from exc
    except EmployerProfileNotFoundError as exc:
        db.rollback()
        raise AppError(
            status_code=404,
            code="EMPLOYER_PROFILE_ACCOUNT_NOT_FOUND",
            message=str(exc),
        ) from exc

    return EmployerProfileRead.model_validate(profile)


@router.get(
    "/me",
    response_model=EmployerProfileRead,
    summary="Get the authenticated user's employer profile",
)
def get_me_employer_profile(
    current_user: CurrentUser,
    db: DatabaseSession,
) -> EmployerProfileRead:
    """Retrieve the employer profile belonging to the authenticated account."""
    try:
        profile: EmployerProfile = get_employer_profile(db, current_user.id)
    except EmployerProfileNotFoundError as exc:
        raise AppError(
            status_code=404,
            code="EMPLOYER_PROFILE_NOT_FOUND",
            message=str(exc),
        ) from exc

    return EmployerProfileRead.model_validate(profile)


@router.patch(
    "/me",
    response_model=EmployerProfileRead,
    summary="Update the authenticated user's employer profile",
)
def update_me_employer_profile(
    payload: EmployerProfileUpdate,
    current_user: CurrentUser,
    db: DatabaseSession,
) -> EmployerProfileRead:
    """Update supplied fields on the authenticated employer profile."""
    updates = payload.model_dump(exclude_unset=True)

    try:
        profile = update_employer_profile(db, current_user.id, updates)
        db.commit()
        db.refresh(profile)
    except EmployerProfileNotFoundError as exc:
        db.rollback()
        raise AppError(
            status_code=404,
            code="EMPLOYER_PROFILE_NOT_FOUND",
            message=str(exc),
        ) from exc

    return EmployerProfileRead.model_validate(profile)
