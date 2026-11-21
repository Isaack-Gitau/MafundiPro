from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import Literal
from uuid import UUID

from sqlalchemy import case, func, or_, select
from sqlalchemy.orm import Session, joinedload, selectinload

from app.models.employer import EmployerProfile
from app.models.job_advertisement import JobAdvertisement
from app.models.location import Location
from app.models.taxonomy import Skill
from app.schemas.job_advertisements import (
    PublicEmployerSummary,
    PublicJobAdvertisementPage,
    PublicJobAdvertisementRead,
    PublicLocationSnapshot,
    PublicProfessionSnapshot,
    PublicSkillSnapshot,
    PublicSubcategorySnapshot,
)

AdvertisementSort = Literal["newest", "oldest", "relevance"]


def _now() -> datetime:
    """Return the current UTC time."""
    return datetime.now(UTC)


def _location_descendants(location_id: UUID):
    """Return a recursive CTE containing a location and all its descendants."""
    descendants = (
        select(Location.id)
        .where(Location.id == location_id)
        .cte(
            name="location_descendants",
            recursive=True,
        )
    )

    child = select(Location.id).join(
        descendants,
        Location.parent_id == descendants.c.id,
    )

    return descendants.union_all(child)


def _apply_filters(
    statement,
    *,
    keyword: str | None,
    job_type: str | None,
    work_arrangement: str | None,
    main_category_id: UUID | None,
    subcategory_id: UUID | None,
    skill_ids: list[UUID] | None,
    location_id: UUID | None,
    compensation_type: str | None,
    payment_unit: str | None,
    min_compensation: Decimal | None,
    max_compensation: Decimal | None,
    published_after: date | None,
    published_before: date | None,
    available_positions_only: bool,
    employer_type: str | None,
):
    """Apply public advertisement search filters."""
    if keyword:
        pattern = f"%{keyword}%"
        statement = statement.where(
            or_(
                JobAdvertisement.title.ilike(pattern),
                JobAdvertisement.description.ilike(pattern),
            )
        )

    if job_type:
        statement = statement.where(
            JobAdvertisement.job_type == job_type,
        )

    if work_arrangement:
        statement = statement.where(
            JobAdvertisement.work_arrangement == work_arrangement,
        )

    if main_category_id:
        statement = statement.where(
            JobAdvertisement.main_category_id == main_category_id,
        )

    if subcategory_id:
        statement = statement.where(
            JobAdvertisement.subcategory_id == subcategory_id,
        )

    if skill_ids:
        for skill_id in set(skill_ids):
            statement = statement.where(
                JobAdvertisement.skills.any(Skill.id == skill_id),
            )

    if location_id:
        descendants = _location_descendants(location_id)

        statement = statement.where(
            JobAdvertisement.location_id.in_(
                select(descendants.c.id),
            ),
        )

    if compensation_type:
        statement = statement.where(
            JobAdvertisement.compensation_type == compensation_type,
        )

    if payment_unit:
        statement = statement.where(
            JobAdvertisement.payment_unit == payment_unit,
        )

    if min_compensation is not None:
        statement = statement.where(
            JobAdvertisement.compensation_type == "fixed",
            or_(
                JobAdvertisement.max_compensation.is_(None),
                JobAdvertisement.max_compensation >= min_compensation,
            ),
        )

    if max_compensation is not None:
        statement = statement.where(
            JobAdvertisement.compensation_type == "fixed",
            or_(
                JobAdvertisement.min_compensation.is_(None),
                JobAdvertisement.min_compensation <= max_compensation,
            ),
        )

    if published_after is not None:
        start = datetime.combine(
            published_after,
            time.min,
            tzinfo=UTC,
        )

        statement = statement.where(
            JobAdvertisement.published_at >= start,
        )

    if published_before is not None:
        end = datetime.combine(
            published_before + timedelta(days=1),
            time.min,
            tzinfo=UTC,
        )

        statement = statement.where(
            JobAdvertisement.published_at < end,
        )

    if available_positions_only:
        statement = statement.where(
            JobAdvertisement.positions_filled < JobAdvertisement.workers_needed,
        )

    if employer_type:
        statement = statement.where(
            JobAdvertisement.employer.has(
                EmployerProfile.employer_type == employer_type,
            ),
        )

    return statement


def _serialize_advertisement(
    advertisement: JobAdvertisement,
    now: datetime,
) -> PublicJobAdvertisementRead:
    """Convert a published advertisement into its safe public representation."""
    if not advertisement.profession_snapshot:
        raise ValueError(
            "A published advertisement is missing its profession snapshot.",
        )

    compensation_is_fixed = advertisement.compensation_type == "fixed"

    contacts_available = advertisement.positions_filled < advertisement.workers_needed and (
        advertisement.response_deadline is None or advertisement.response_deadline > now
    )

    contact_methods = (
        sorted(method.contact_method for method in advertisement.contact_methods)
        if contacts_available
        else []
    )

    return PublicJobAdvertisementRead(
        id=advertisement.id,
        title=advertisement.title,
        description=advertisement.description,
        job_type=advertisement.job_type,
        work_arrangement=advertisement.work_arrangement,
        profession=PublicProfessionSnapshot.model_validate(
            advertisement.profession_snapshot,
        ),
        subcategory=(
            PublicSubcategorySnapshot.model_validate(
                advertisement.subcategory_snapshot,
            )
            if advertisement.subcategory_snapshot
            else None
        ),
        required_skills=[
            PublicSkillSnapshot.model_validate(skill)
            for skill in (advertisement.skills_snapshot or [])
        ],
        location=(
            PublicLocationSnapshot.model_validate(
                advertisement.location_snapshot,
            )
            if advertisement.location_snapshot
            else None
        ),
        compensation_type=advertisement.compensation_type,
        min_compensation=(advertisement.min_compensation if compensation_is_fixed else None),
        max_compensation=(advertisement.max_compensation if compensation_is_fixed else None),
        payment_unit=(advertisement.payment_unit if compensation_is_fixed else None),
        currency_code=(advertisement.currency_code if compensation_is_fixed else None),
        workers_needed=advertisement.workers_needed,
        positions_filled=advertisement.positions_filled,
        remaining_positions=(advertisement.workers_needed - advertisement.positions_filled),
        start_date=advertisement.start_date,
        response_deadline=advertisement.response_deadline,
        published_at=advertisement.published_at,
        expires_at=advertisement.expires_at,
        employer=PublicEmployerSummary(
            display_name=advertisement.employer.display_name,
            employer_type=advertisement.employer.employer_type,
        ),
        available_contact_methods=contact_methods,
    )


def search_public_job_advertisements(
    db: Session,
    *,
    keyword: str | None = None,
    job_type: str | None = None,
    work_arrangement: str | None = None,
    main_category_id: UUID | None = None,
    subcategory_id: UUID | None = None,
    skill_ids: list[UUID] | None = None,
    location_id: UUID | None = None,
    compensation_type: str | None = None,
    payment_unit: str | None = None,
    min_compensation: Decimal | None = None,
    max_compensation: Decimal | None = None,
    published_after: date | None = None,
    published_before: date | None = None,
    available_positions_only: bool = False,
    employer_type: str | None = None,
    sort: AdvertisementSort = "newest",
    page: int = 1,
    page_size: int = 20,
) -> PublicJobAdvertisementPage:
    """Search published, unexpired advertisements with database pagination."""
    if page < 1:
        raise ValueError("Page must be at least 1.")

    if not 1 <= page_size <= 100:
        raise ValueError("Page size must be between 1 and 100.")

    if sort not in ("newest", "oldest", "relevance"):
        raise ValueError("Unsupported advertisement sorting option.")

    normalized_keyword = keyword.strip() if keyword else None

    if normalized_keyword == "":
        normalized_keyword = None

    if sort == "relevance" and normalized_keyword is None:
        raise ValueError("Relevance sorting requires a keyword.")

    if (
        min_compensation is not None
        and max_compensation is not None
        and min_compensation > max_compensation
    ):
        raise ValueError(
            "Minimum compensation cannot exceed maximum compensation.",
        )

    now = _now()

    statement = select(JobAdvertisement).where(
        JobAdvertisement.status == "published",
        JobAdvertisement.expires_at > now,
    )

    statement = _apply_filters(
        statement,
        keyword=normalized_keyword,
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
    )

    total = (
        db.scalar(
            select(func.count()).select_from(
                statement.order_by(None).subquery(),
            ),
        )
        or 0
    )

    if sort == "oldest":
        statement = statement.order_by(
            JobAdvertisement.published_at.asc(),
            JobAdvertisement.id.asc(),
        )

    elif sort == "relevance":
        pattern = f"%{normalized_keyword}%"

        relevance = case(
            (JobAdvertisement.title.ilike(pattern), 0),
            (JobAdvertisement.description.ilike(pattern), 1),
            else_=2,
        )

        statement = statement.order_by(
            relevance.asc(),
            JobAdvertisement.published_at.desc(),
            JobAdvertisement.id.asc(),
        )

    else:
        statement = statement.order_by(
            JobAdvertisement.published_at.desc(),
            JobAdvertisement.id.asc(),
        )

    statement = (
        statement.options(
            joinedload(JobAdvertisement.employer),
            selectinload(JobAdvertisement.contact_methods),
        )
        .offset((page - 1) * page_size)
        .limit(page_size)
    )

    advertisements = db.scalars(statement).all()

    return PublicJobAdvertisementPage(
        items=[_serialize_advertisement(advertisement, now) for advertisement in advertisements],
        page=page,
        page_size=page_size,
        total=total,
        pages=(total + page_size - 1) // page_size,
    )


def get_public_job_advertisement(
    db: Session,
    advertisement_id: UUID,
) -> PublicJobAdvertisementRead | None:
    """Retrieve one published, unexpired advertisement by its public identifier."""
    now = _now()

    statement = (
        select(JobAdvertisement)
        .where(
            JobAdvertisement.id == advertisement_id,
            JobAdvertisement.status == "published",
            JobAdvertisement.expires_at > now,
        )
        .options(
            joinedload(JobAdvertisement.employer),
            selectinload(JobAdvertisement.contact_methods),
        )
    )

    advertisement = db.scalar(statement)

    if advertisement is None:
        return None

    return _serialize_advertisement(advertisement, now)
