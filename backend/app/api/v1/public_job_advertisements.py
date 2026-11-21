from datetime import date
from decimal import Decimal
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, status

from app.api.dependencies.database import DatabaseSession
from app.schemas.job_advertisements import (
    PublicJobAdvertisementPage,
    PublicJobAdvertisementRead,
)
from app.services.public_job_advertisements import (
    get_public_job_advertisement,
    search_public_job_advertisements,
)

router = APIRouter()


@router.get(
    "/job-advertisements",
    response_model=PublicJobAdvertisementPage,
    summary="Search public job advertisements",
)
def list_public_job_advertisements(
    db: DatabaseSession,
    keyword: Annotated[
        str | None,
        Query(min_length=1, max_length=200),
    ] = None,
    job_type: Literal[
        "casual_daily",
        "temporary",
        "contract_project",
        "part_time",
        "full_time_permanent",
    ]
    | None = None,
    work_arrangement: Literal["on_site", "remote", "hybrid"] | None = None,
    main_category_id: UUID | None = None,
    subcategory_id: UUID | None = None,
    skill_ids: Annotated[list[UUID] | None, Query()] = None,
    location_id: UUID | None = None,
    compensation_type: Literal["fixed", "negotiable", "undisclosed"] | None = None,
    payment_unit: Literal["hourly", "daily", "weekly", "monthly", "fixed"] | None = None,
    min_compensation: Annotated[Decimal | None, Query(ge=0)] = None,
    max_compensation: Annotated[Decimal | None, Query(ge=0)] = None,
    published_after: date | None = None,
    published_before: date | None = None,
    available_positions_only: bool = False,
    employer_type: Literal[
        "individual",
        "business",
        "organization",
        "contractor",
    ]
    | None = None,
    sort: Literal["newest", "oldest", "relevance"] = "newest",
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
) -> PublicJobAdvertisementPage:
    """Search published and unexpired advertisements using public filters."""
    if (
        published_after is not None
        and published_before is not None
        and published_after > published_before
    ):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Publication start date cannot be later than end date.",
        )

    if (
        min_compensation is not None
        and max_compensation is not None
        and min_compensation > max_compensation
    ):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Minimum compensation cannot exceed maximum compensation.",
        )

    if sort == "relevance" and not (keyword and keyword.strip()):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Relevance sorting requires a keyword.",
        )

    try:
        return search_public_job_advertisements(
            db,
            keyword=keyword,
            job_type=job_type,
            work_arrangement=work_arrangement,
            main_category_id=main_category_id,
            subcategory_id=subcategory_id,
            skill_ids=skill_ids,
            location_id=location_id,
            compensation_type=compensation_type,
            payment_unit=payment_unit,
            min_compensation=min_compensation,
            max_compensation=max_compensation,
            published_after=published_after,
            published_before=published_before,
            available_positions_only=available_positions_only,
            employer_type=employer_type,
            sort=sort,
            page=page,
            page_size=page_size,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc


@router.get(
    "/job-advertisements/{advertisement_id}",
    response_model=PublicJobAdvertisementRead,
    summary="View a public job advertisement",
)
def read_public_job_advertisement(
    advertisement_id: UUID,
    db: DatabaseSession,
) -> PublicJobAdvertisementRead:
    """Retrieve one published and unexpired advertisement."""
    advertisement = get_public_job_advertisement(db, advertisement_id)

    if advertisement is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Job advertisement not found.",
        )

    return advertisement
