from collections.abc import Callable
from uuid import UUID

from fastapi import APIRouter
from sqlalchemy.exc import IntegrityError

from app.api.dependencies.authorization import AdminUser, ModeratorOrAdmin
from app.api.dependencies.database import DatabaseSession
from app.core.errors import AppError
from app.schemas.job_advertisement_moderation import (
    JobAdvertisementModerationRead,
    JobAdvertisementModerationReason,
)
from app.services.job_advertisements import (
    JobAdvertisementConflictError,
    JobAdvertisementNotFoundError,
    JobAdvertisementValidationError,
    reinstate_job_advertisement,
    remove_job_advertisement,
    suspend_job_advertisement,
)

router = APIRouter()


def _run_moderation_write[T](
    db: DatabaseSession,
    operation: Callable[[], T],
) -> T:
    """Run a moderation operation and manage its database transaction."""
    try:
        result = operation()
        db.commit()
        return result
    except JobAdvertisementNotFoundError as exc:
        db.rollback()
        raise AppError(
            status_code=404,
            code="JOB_ADVERTISEMENT_NOT_FOUND",
            message=str(exc),
        ) from exc
    except JobAdvertisementValidationError as exc:
        db.rollback()
        raise AppError(
            status_code=422,
            code="INVALID_JOB_ADVERTISEMENT_MODERATION",
            message=str(exc),
        ) from exc
    except JobAdvertisementConflictError as exc:
        db.rollback()
        raise AppError(
            status_code=409,
            code="JOB_ADVERTISEMENT_MODERATION_CONFLICT",
            message=str(exc),
        ) from exc
    except IntegrityError as exc:
        db.rollback()
        raise AppError(
            status_code=409,
            code="JOB_ADVERTISEMENT_MODERATION_CONFLICT",
            message="The moderation operation conflicts with existing data.",
        ) from exc


def _serialize_moderation_result(
    advertisement,
    reason: str,
) -> JobAdvertisementModerationRead:
    """Build the safe response for a moderation action."""
    return JobAdvertisementModerationRead(
        id=advertisement.id,
        status=advertisement.status,
        reason=reason,
        published_at=advertisement.published_at,
        expires_at=advertisement.expires_at,
        updated_at=advertisement.updated_at,
    )


@router.post(
    "/{advertisement_id}/suspend",
    response_model=JobAdvertisementModerationRead,
    summary="Suspend a published job advertisement",
)
def admin_suspend_job_advertisement(
    advertisement_id: UUID,
    payload: JobAdvertisementModerationReason,
    _: ModeratorOrAdmin,
    db: DatabaseSession,
) -> JobAdvertisementModerationRead:
    advertisement = _run_moderation_write(
        db,
        lambda: suspend_job_advertisement(
            db,
            _.id,
            advertisement_id,
            payload.reason,
        ),
    )
    return _serialize_moderation_result(advertisement, payload.reason)


@router.post(
    "/{advertisement_id}/reinstate",
    response_model=JobAdvertisementModerationRead,
    summary="Reinstate a suspended job advertisement",
)
def admin_reinstate_job_advertisement(
    advertisement_id: UUID,
    payload: JobAdvertisementModerationReason,
    _: ModeratorOrAdmin,
    db: DatabaseSession,
) -> JobAdvertisementModerationRead:
    advertisement = _run_moderation_write(
        db,
        lambda: reinstate_job_advertisement(
            db,
            _.id,
            advertisement_id,
            payload.reason,
        ),
    )
    return _serialize_moderation_result(advertisement, payload.reason)


@router.post(
    "/{advertisement_id}/remove",
    response_model=JobAdvertisementModerationRead,
    summary="Remove a job advertisement",
)
def admin_remove_job_advertisement(
    advertisement_id: UUID,
    payload: JobAdvertisementModerationReason,
    _: AdminUser,
    db: DatabaseSession,
) -> JobAdvertisementModerationRead:
    advertisement = _run_moderation_write(
        db,
        lambda: remove_job_advertisement(
            db,
            _.id,
            advertisement_id,
            payload.reason,
        ),
    )
    return _serialize_moderation_result(advertisement, payload.reason)
