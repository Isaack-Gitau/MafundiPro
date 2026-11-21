from math import ceil
from typing import Literal, cast
from uuid import UUID

from fastapi import APIRouter, Query, status
from sqlalchemy import select

from app.api.dependencies.authentication import CurrentUser
from app.api.dependencies.database import DatabaseSession
from app.core.errors import AppError
from app.models.job_advertisement import (
    JobAdvertisement,
    JobAdvertisementAudit,
)
from app.schemas.employer_job_advertisements import (
    EmployerAdvertisementCreate,
    EmployerAdvertisementPositionsUpdate,
    EmployerAdvertisementUpdate,
    EmployerJobAdvertisementPage,
    EmployerJobAdvertisementRead,
    EmployerPostingCreditBalance,
)
from app.schemas.job_advertisements import (
    CompensationType,
    ContactMethod,
    JobType,
    PaymentUnit,
    WorkArrangement,
)
from app.services.job_advertisements import (
    InsufficientPostingCreditsError,
    JobAdvertisementConflictError,
    JobAdvertisementError,
    JobAdvertisementNotFoundError,
    JobAdvertisementValidationError,
    close_job_advertisement,
    create_job_advertisement_draft,
    get_employer_job_advertisement,
    get_employer_posting_credit_summary,
    list_employer_job_advertisements,
    publish_job_advertisement,
    update_job_advertisement,
    update_job_advertisement_positions_filled,
)

router = APIRouter()


def _translate_job_advertisement_error(exc: JobAdvertisementError) -> AppError:
    """Translate job advertisement service errors into API errors."""
    if isinstance(exc, JobAdvertisementNotFoundError):
        return AppError(
            status_code=404,
            code="JOB_ADVERTISEMENT_NOT_FOUND",
            message=str(exc),
        )

    if isinstance(exc, InsufficientPostingCreditsError):
        return AppError(
            status_code=409,
            code="INSUFFICIENT_POSTING_CREDITS",
            message=str(exc),
        )

    if isinstance(exc, JobAdvertisementConflictError):
        return AppError(
            status_code=409,
            code="JOB_ADVERTISEMENT_CONFLICT",
            message=str(exc),
        )

    if isinstance(exc, JobAdvertisementValidationError):
        return AppError(
            status_code=422,
            code="INVALID_JOB_ADVERTISEMENT",
            message=str(exc),
        )

    return AppError(
        status_code=400,
        code="JOB_ADVERTISEMENT_ERROR",
        message=str(exc),
    )


def _serialize_advertisement(
    db: DatabaseSession,
    advertisement: JobAdvertisement,
) -> EmployerJobAdvertisementRead:
    """Serialize an employer's advertisement management representation."""
    moderation_reason = None

    audit_action = {
        "suspended": "suspend",
        "removed": "remove",
    }.get(advertisement.status)

    if audit_action is not None:
        moderation_reason = db.scalar(
            select(JobAdvertisementAudit.reason)
            .where(
                JobAdvertisementAudit.advertisement_id == advertisement.id,
                JobAdvertisementAudit.action == audit_action,
            )
            .order_by(
                JobAdvertisementAudit.created_at.desc(),
                JobAdvertisementAudit.id.desc(),
            )
            .limit(1)
        )

    remaining_positions = (
        advertisement.workers_needed - advertisement.positions_filled
        if advertisement.workers_needed is not None
        else None
    )

    return EmployerJobAdvertisementRead(
        id=advertisement.id,
        title=advertisement.title,
        description=advertisement.description,
        job_type=cast(JobType | None, advertisement.job_type),
        work_arrangement=cast(
            WorkArrangement | None,
            advertisement.work_arrangement,
        ),
        main_category_id=advertisement.main_category_id,
        subcategory_id=advertisement.subcategory_id,
        location_id=advertisement.location_id,
        skill_ids=sorted(
            (skill.id for skill in advertisement.skills),
            key=str,
        ),
        profession_snapshot=advertisement.profession_snapshot,
        subcategory_snapshot=advertisement.subcategory_snapshot,
        skills_snapshot=advertisement.skills_snapshot,
        location_snapshot=advertisement.location_snapshot,
        compensation_type=cast(
            CompensationType | None,
            advertisement.compensation_type,
        ),
        min_compensation=advertisement.min_compensation,
        max_compensation=advertisement.max_compensation,
        payment_unit=cast(
            PaymentUnit | None,
            advertisement.payment_unit,
        ),
        currency_code=advertisement.currency_code,
        workers_needed=advertisement.workers_needed,
        positions_filled=advertisement.positions_filled,
        remaining_positions=remaining_positions,
        start_date=advertisement.start_date,
        response_deadline=advertisement.response_deadline,
        contact_methods=cast(
            list[ContactMethod],
            sorted(method.contact_method for method in advertisement.contact_methods),
        ),
        status=cast(
            Literal[
                "draft",
                "published",
                "suspended",
                "expired",
                "removed",
                "closed",
            ],
            advertisement.status,
        ),
        moderation_reason=moderation_reason,
        published_at=advertisement.published_at,
        expires_at=advertisement.expires_at,
        closed_at=advertisement.closed_at,
        created_at=advertisement.created_at,
        updated_at=advertisement.updated_at,
    )


@router.get(
    "/credits/balance",
    response_model=EmployerPostingCreditBalance,
    summary="Get employer advertisement posting credits",
)
def get_my_posting_credit_balance(
    current_user: CurrentUser,
    db: DatabaseSession,
) -> EmployerPostingCreditBalance:
    """Get posting credits and configured prices for the current employer."""
    try:
        summary = get_employer_posting_credit_summary(db, current_user.id)
    except JobAdvertisementError as exc:
        raise _translate_job_advertisement_error(exc) from exc

    return EmployerPostingCreditBalance(**summary)


@router.post(
    "",
    response_model=EmployerJobAdvertisementRead,
    status_code=status.HTTP_201_CREATED,
    summary="Create an employer advertisement draft",
)
def create_my_advertisement(
    payload: EmployerAdvertisementCreate,
    current_user: CurrentUser,
    db: DatabaseSession,
) -> EmployerJobAdvertisementRead:
    """Create an incomplete draft for the authenticated employer."""
    try:
        advertisement = create_job_advertisement_draft(
            db,
            current_user.id,
            payload.model_dump(exclude_unset=True),
        )
        db.commit()
    except JobAdvertisementError as exc:
        db.rollback()
        raise _translate_job_advertisement_error(exc) from exc

    return _serialize_advertisement(db, advertisement)


@router.get(
    "",
    response_model=EmployerJobAdvertisementPage,
    summary="List the authenticated employer's advertisements",
)
def list_my_advertisements(
    current_user: CurrentUser,
    db: DatabaseSession,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    status_filter: str | None = Query(default=None, alias="status"),
) -> EmployerJobAdvertisementPage:
    """List advertisements belonging to the authenticated employer."""
    try:
        advertisements, total = list_employer_job_advertisements(
            db,
            current_user.id,
            page=page,
            page_size=page_size,
            status_filter=status_filter,
        )
    except JobAdvertisementError as exc:
        raise _translate_job_advertisement_error(exc) from exc

    return EmployerJobAdvertisementPage(
        items=[_serialize_advertisement(db, advertisement) for advertisement in advertisements],
        page=page,
        page_size=page_size,
        total=total,
        pages=ceil(total / page_size) if total else 0,
    )


@router.get(
    "/{advertisement_id}",
    response_model=EmployerJobAdvertisementRead,
    summary="Get one employer advertisement",
)
def get_my_advertisement(
    advertisement_id: UUID,
    current_user: CurrentUser,
    db: DatabaseSession,
) -> EmployerJobAdvertisementRead:
    """Retrieve an advertisement owned by the authenticated employer."""
    try:
        advertisement = get_employer_job_advertisement(
            db,
            current_user.id,
            advertisement_id,
        )
    except JobAdvertisementError as exc:
        raise _translate_job_advertisement_error(exc) from exc

    return _serialize_advertisement(db, advertisement)


@router.patch(
    "/{advertisement_id}",
    response_model=EmployerJobAdvertisementRead,
    summary="Update an employer advertisement",
)
def update_my_advertisement(
    advertisement_id: UUID,
    payload: EmployerAdvertisementUpdate,
    current_user: CurrentUser,
    db: DatabaseSession,
) -> EmployerJobAdvertisementRead:
    """Update an employer-owned draft or published advertisement."""
    try:
        advertisement = update_job_advertisement(
            db,
            current_user.id,
            advertisement_id,
            payload.model_dump(exclude_unset=True),
        )
        db.commit()
    except JobAdvertisementError as exc:
        db.rollback()
        raise _translate_job_advertisement_error(exc) from exc

    return _serialize_advertisement(db, advertisement)


@router.post(
    "/{advertisement_id}/publish",
    response_model=EmployerJobAdvertisementRead,
    summary="Publish an employer advertisement",
)
def publish_my_advertisement(
    advertisement_id: UUID,
    current_user: CurrentUser,
    db: DatabaseSession,
) -> EmployerJobAdvertisementRead:
    """Publish a draft after validation and credit verification."""
    try:
        advertisement = publish_job_advertisement(
            db,
            current_user.id,
            advertisement_id,
        )
        db.commit()
    except JobAdvertisementError as exc:
        db.rollback()
        raise _translate_job_advertisement_error(exc) from exc

    return _serialize_advertisement(db, advertisement)


@router.post(
    "/{advertisement_id}/close",
    response_model=EmployerJobAdvertisementRead,
    summary="Close an employer advertisement",
)
def close_my_advertisement(
    advertisement_id: UUID,
    current_user: CurrentUser,
    db: DatabaseSession,
) -> EmployerJobAdvertisementRead:
    """Close an employer-owned published advertisement."""
    try:
        advertisement = close_job_advertisement(
            db,
            current_user.id,
            advertisement_id,
        )
        db.commit()
    except JobAdvertisementError as exc:
        db.rollback()
        raise _translate_job_advertisement_error(exc) from exc

    return _serialize_advertisement(db, advertisement)


@router.patch(
    "/{advertisement_id}/positions",
    response_model=EmployerJobAdvertisementRead,
    summary="Update filled advertisement positions",
)
def update_my_advertisement_positions(
    advertisement_id: UUID,
    payload: EmployerAdvertisementPositionsUpdate,
    current_user: CurrentUser,
    db: DatabaseSession,
) -> EmployerJobAdvertisementRead:
    """Update the number of positions filled on a published advertisement."""
    try:
        advertisement = update_job_advertisement_positions_filled(
            db,
            current_user.id,
            advertisement_id,
            payload.positions_filled,
        )
        db.commit()
    except JobAdvertisementError as exc:
        db.rollback()
        raise _translate_job_advertisement_error(exc) from exc

    return _serialize_advertisement(db, advertisement)
