from uuid import UUID

from fastapi import APIRouter, status
from sqlalchemy.exc import IntegrityError

from app.api.dependencies.authentication import CurrentUser
from app.api.dependencies.database import DatabaseSession
from app.core.errors import AppError
from app.models.worker import WorkerProfile
from app.schemas.workers import (
    WorkerProfessionSelectionInput,
    WorkerProfessionSelectionsRead,
    WorkerProfileRead,
    WorkerProfileUpdate,
    WorkerSkillSelectionsInput,
)
from app.services.taxonomy import TaxonomyNotFoundError, TaxonomyValidationError
from app.services.workers import (
    WorkerProfileConflictError,
    WorkerProfileNotFoundError,
    create_worker_profile,
    get_worker_profession_selections,
    get_worker_profile,
    replace_worker_professions,
    replace_worker_skills,
    update_worker_profile,
)

router = APIRouter()


def _raise_taxonomy_error(exc: TaxonomyNotFoundError | TaxonomyValidationError) -> None:
    """Translate taxonomy service errors into standard API errors."""
    if isinstance(exc, TaxonomyNotFoundError):
        raise AppError(
            status_code=404,
            code="TAXONOMY_RESOURCE_NOT_FOUND",
            message=str(exc),
        ) from exc

    raise AppError(
        status_code=422,
        code="INVALID_WORKER_SELECTION",
        message=str(exc),
    ) from exc


def _raise_selection_integrity_error(exc: IntegrityError) -> None:
    """Translate selection database conflicts without exposing database details."""
    raise AppError(
        status_code=409,
        code="WORKER_SELECTION_CONFLICT",
        message="The worker selections conflict with the current data. Refresh and try again.",
    ) from exc


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


@router.patch(
    "/me",
    response_model=WorkerProfileRead,
    summary="Update the authenticated user's worker profile",
)
def update_me_worker_profile(
    payload: WorkerProfileUpdate,
    current_user: CurrentUser,
    db: DatabaseSession,
) -> WorkerProfileRead:
    """Update the supplied fields on the authenticated worker profile."""
    updates = payload.model_dump(exclude_unset=True)

    try:
        profile = update_worker_profile(db, current_user.id, updates)
        db.commit()
        db.refresh(profile)
    except WorkerProfileNotFoundError as exc:
        db.rollback()
        raise AppError(
            status_code=404,
            code="WORKER_PROFILE_NOT_FOUND",
            message=str(exc),
        ) from exc

    return WorkerProfileRead.model_validate(profile)


@router.get(
    "/me/professions",
    response_model=WorkerProfessionSelectionsRead,
    summary="Get the authenticated worker's professions and skills",
)
def get_me_worker_professions(
    current_user: CurrentUser,
    db: DatabaseSession,
) -> WorkerProfessionSelectionsRead:
    """Retrieve the authenticated worker's selected professions and skills."""
    try:
        selections = get_worker_profession_selections(db, current_user.id)
    except WorkerProfileNotFoundError as exc:
        raise AppError(
            status_code=404,
            code="WORKER_PROFILE_NOT_FOUND",
            message=str(exc),
        ) from exc

    return WorkerProfessionSelectionsRead.model_validate(selections)


@router.put(
    "/me/professions",
    response_model=WorkerProfessionSelectionsRead,
    summary="Replace the authenticated worker's main professions",
)
def replace_me_worker_professions(
    payload: WorkerProfessionSelectionInput,
    current_user: CurrentUser,
    db: DatabaseSession,
) -> WorkerProfessionSelectionsRead:
    """Replace main professions while retaining skills for professions kept."""
    try:
        selections = replace_worker_professions(
            db,
            current_user.id,
            payload.category_ids,
        )
        db.commit()
    except WorkerProfileNotFoundError as exc:
        db.rollback()
        raise AppError(
            status_code=404,
            code="WORKER_PROFILE_NOT_FOUND",
            message=str(exc),
        ) from exc
    except TaxonomyNotFoundError as exc:
        db.rollback()
        _raise_taxonomy_error(exc)
    except TaxonomyValidationError as exc:
        db.rollback()
        _raise_taxonomy_error(exc)
    except IntegrityError as exc:
        db.rollback()
        _raise_selection_integrity_error(exc)

    return WorkerProfessionSelectionsRead.model_validate(selections)


@router.put(
    "/me/professions/{category_id}/skills",
    response_model=WorkerProfessionSelectionsRead,
    summary="Replace skills selected for one main profession",
)
def replace_me_worker_skills(
    category_id: UUID,
    payload: WorkerSkillSelectionsInput,
    current_user: CurrentUser,
    db: DatabaseSession,
) -> WorkerProfessionSelectionsRead:
    """Replace the selected skills for one of the worker's main professions."""
    selections_input = [selection.model_dump() for selection in payload.skills]

    try:
        selections = replace_worker_skills(
            db,
            current_user.id,
            category_id,
            selections_input,
        )
        db.commit()
    except WorkerProfileNotFoundError as exc:
        db.rollback()
        raise AppError(
            status_code=404,
            code="WORKER_PROFILE_NOT_FOUND",
            message=str(exc),
        ) from exc
    except TaxonomyNotFoundError as exc:
        db.rollback()
        _raise_taxonomy_error(exc)
    except TaxonomyValidationError as exc:
        db.rollback()
        _raise_taxonomy_error(exc)
    except IntegrityError as exc:
        db.rollback()
        _raise_selection_integrity_error(exc)

    return WorkerProfessionSelectionsRead.model_validate(selections)
