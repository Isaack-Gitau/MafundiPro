from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app.models.employer import EmployerProfile
from app.models.identity import User
from app.models.job_advertisement import (
    EmployerPostingCreditEntry,
    JobAdSettings,
    JobAdvertisement,
    JobAdvertisementAudit,
    JobAdvertisementContactMethod,
)
from app.models.location import Location
from app.models.taxonomy import Category, Skill, category_skills
from app.services.location_lock import lock_location_mutations
from app.services.locations import (
    LocationError,
    validate_active_location_chain,
)
from app.services.taxonomy import (
    TaxonomyError,
    validate_category_active_chain,
)
from app.services.taxonomy_lock import lock_taxonomy_mutations

KENYA_TIMEZONE = ZoneInfo("Africa/Nairobi")

JOB_TYPES = {
    "casual_daily",
    "temporary",
    "contract_project",
    "part_time",
    "full_time_permanent",
}
WORK_ARRANGEMENTS = {"on_site", "remote", "hybrid"}
COMPENSATION_TYPES = {"fixed", "negotiable", "undisclosed"}
PAYMENT_UNITS = {"hourly", "daily", "weekly", "monthly", "fixed"}
CONTACT_METHODS = {"phone_call", "whatsapp", "in_app_message"}

MUTABLE_FIELDS = {
    "title",
    "description",
    "job_type",
    "work_arrangement",
    "main_category_id",
    "subcategory_id",
    "location_id",
    "compensation_type",
    "min_compensation",
    "max_compensation",
    "payment_unit",
    "currency_code",
    "workers_needed",
    "start_date",
    "response_deadline",
}


class JobAdvertisementError(Exception):
    """Base exception for job advertisement business rules."""


class JobAdvertisementNotFoundError(JobAdvertisementError):
    """Raised when an advertisement cannot be found."""


class JobAdvertisementValidationError(JobAdvertisementError):
    """Raised when advertisement data violates business rules."""


class JobAdvertisementConflictError(JobAdvertisementError):
    """Raised when an advertisement operation conflicts with its state."""


class InsufficientPostingCreditsError(JobAdvertisementError):
    """Raised when an employer has no available posting credits."""


def _now() -> datetime:
    return datetime.now(UTC)


def _today_in_kenya() -> date:
    return datetime.now(KENYA_TIMEZONE).date()


def _get_active_employer_profile(db: Session, user_id: UUID) -> EmployerProfile:
    """Retrieve and lock the authenticated user's active employer profile."""
    employer = db.scalar(
        select(EmployerProfile).where(EmployerProfile.user_id == user_id).with_for_update()
    )

    if employer is None:
        raise JobAdvertisementNotFoundError(
            "Create an employer profile before managing job advertisements."
        )

    user = db.get(User, user_id)

    if user is None or user.account_status != "active":
        raise JobAdvertisementValidationError(
            "An active account is required for employer operations."
        )

    if user.phone_verified_at is None:
        raise JobAdvertisementValidationError(
            "Verify your phone number before managing job advertisements."
        )

    return employer


def _get_owned_advertisement(
    db: Session,
    employer_profile_id: UUID,
    advertisement_id: UUID,
) -> JobAdvertisement:
    """Retrieve and lock an advertisement belonging to the employer."""
    advertisement = db.scalar(
        select(JobAdvertisement)
        .where(
            JobAdvertisement.id == advertisement_id,
            JobAdvertisement.employer_profile_id == employer_profile_id,
        )
        .options(
            selectinload(JobAdvertisement.contact_methods),
            selectinload(JobAdvertisement.skills),
        )
        .with_for_update()
    )

    if advertisement is None:
        raise JobAdvertisementNotFoundError("Job advertisement not found.")

    return advertisement


def _validate_enum(
    value: str | None,
    allowed: set[str],
    field_name: str,
) -> None:
    if value is not None and value not in allowed:
        raise JobAdvertisementValidationError(f"Invalid {field_name.replace('_', ' ')}.")


def _normalize_values(values: dict) -> dict:
    """Validate individual fields without requiring a complete draft."""
    unknown = set(values) - MUTABLE_FIELDS - {"skill_ids", "contact_methods"}

    if unknown:
        raise JobAdvertisementValidationError(
            f"Unsupported advertisement fields: {', '.join(sorted(unknown))}."
        )

    normalized = dict(values)

    for field in ("title", "description", "currency_code"):
        if field in normalized and isinstance(normalized[field], str):
            normalized[field] = normalized[field].strip()

    _validate_enum(normalized.get("job_type"), JOB_TYPES, "job_type")
    _validate_enum(
        normalized.get("work_arrangement"),
        WORK_ARRANGEMENTS,
        "work_arrangement",
    )
    _validate_enum(
        normalized.get("compensation_type"),
        COMPENSATION_TYPES,
        "compensation_type",
    )
    _validate_enum(
        normalized.get("payment_unit"),
        PAYMENT_UNITS,
        "payment_unit",
    )

    for field in ("min_compensation", "max_compensation"):
        value = normalized.get(field)
        if value is not None:
            try:
                normalized[field] = Decimal(str(value))
            except (ValueError, TypeError, ArithmeticError) as exc:
                raise JobAdvertisementValidationError(
                    f"{field.replace('_', ' ').capitalize()} must be a valid amount."
                ) from exc

            if not normalized[field].is_finite() or normalized[field] < 0:
                raise JobAdvertisementValidationError(
                    f"{field.replace('_', ' ').capitalize()} cannot be negative."
                )

    if "workers_needed" in normalized:
        value = normalized["workers_needed"]
        if value is not None and (
            isinstance(value, bool) or not isinstance(value, int) or value < 1
        ):
            raise JobAdvertisementValidationError(
                "The number of workers needed must be a positive whole number."
            )

    if "currency_code" in normalized:
        currency = normalized["currency_code"]
        if not isinstance(currency, str) or len(currency) != 3 or not currency.isalpha():
            raise JobAdvertisementValidationError("Currency code must contain three letters.")
        normalized["currency_code"] = currency.upper()

    if "response_deadline" in normalized:
        deadline = normalized["response_deadline"]
        if deadline is not None and (
            not isinstance(deadline, datetime)
            or deadline.tzinfo is None
            or deadline.utcoffset() is None
        ):
            raise JobAdvertisementValidationError("Response deadline must include a timezone.")

    for field in ("main_category_id", "subcategory_id", "location_id"):
        value = normalized.get(field)
        if value is not None and not isinstance(value, UUID):
            raise JobAdvertisementValidationError(
                f"{field.replace('_', ' ').capitalize()} must be a valid UUID."
            )

    if "start_date" in normalized:
        value = normalized["start_date"]
        if value is not None and (not isinstance(value, date) or isinstance(value, datetime)):
            raise JobAdvertisementValidationError("Start date must be a valid date.")

    if "skill_ids" in normalized:
        skill_ids = normalized["skill_ids"]
        if not isinstance(skill_ids, (list, tuple, set)):
            raise JobAdvertisementValidationError("Required skills must be provided as a list.")
        if any(not isinstance(item, UUID) for item in skill_ids):
            raise JobAdvertisementValidationError("Every required skill must have a valid UUID.")
        if len(skill_ids) != len(set(skill_ids)):
            raise JobAdvertisementValidationError("Duplicate required skills are not allowed.")
        normalized["skill_ids"] = list(skill_ids)

    if "contact_methods" in normalized:
        methods = normalized["contact_methods"]
        if not isinstance(methods, (list, tuple, set)):
            raise JobAdvertisementValidationError("Contact methods must be provided as a list.")
        if any(not isinstance(item, str) for item in methods):
            raise JobAdvertisementValidationError("Every contact method must be a string.")
        if len(methods) != len(set(methods)):
            raise JobAdvertisementValidationError("Duplicate contact methods are not allowed.")
        if not set(methods).issubset(CONTACT_METHODS):
            raise JobAdvertisementValidationError("One or more contact methods are invalid.")
        normalized["contact_methods"] = list(methods)

    return normalized


def _apply_values(advertisement: JobAdvertisement, values: dict) -> None:
    """Apply only ordinary mapped fields to the advertisement."""
    for field in MUTABLE_FIELDS:
        if field in values:
            setattr(advertisement, field, values[field])


def _replace_contact_methods(
    advertisement: JobAdvertisement,
    methods: list[str],
) -> None:
    advertisement.contact_methods.clear()
    advertisement.contact_methods.extend(
        JobAdvertisementContactMethod(contact_method=method) for method in methods
    )


def _get_category_path(db: Session, category_id: UUID) -> list[Category]:
    """Return a category path from its root to the selected category."""
    current = db.get(Category, category_id)
    if current is None:
        raise JobAdvertisementValidationError(
            "The selected profession or subcategory does not exist."
        )

    path = []
    visited = set()

    while current is not None:
        if current.id in visited:
            raise JobAdvertisementValidationError(
                "The profession hierarchy contains a circular reference."
            )

        visited.add(current.id)
        path.append(current)

        if current.parent_id is None:
            break

        current = db.get(Category, current.parent_id)
        if current is None:
            raise JobAdvertisementValidationError(
                "The profession hierarchy contains a missing parent."
            )

    path.reverse()
    return path


def _get_descendant_category_ids(db: Session, root_id: UUID) -> set[UUID]:
    """Return a root category and its active descendants, skipping inactive branches."""
    result = {root_id}
    pending = [root_id]

    while pending:
        parent_id = pending.pop()
        children = db.scalars(
            select(Category.id).where(
                Category.parent_id == parent_id,
                Category.is_active.is_(True),
            )
        ).all()

        for child_id in children:
            if child_id not in result:
                result.add(child_id)
                pending.append(child_id)

    return result


def _get_location_path(db: Session, location_id: UUID) -> list[Location]:
    """Return a location hierarchy from its root to the selected location."""
    current = db.get(Location, location_id)
    if current is None:
        raise JobAdvertisementValidationError("The selected work location does not exist.")

    path = []
    visited = set()

    while current is not None:
        if current.id in visited:
            raise JobAdvertisementValidationError(
                "The location hierarchy contains a circular reference."
            )

        visited.add(current.id)
        path.append(current)

        if current.parent_id is None:
            break

        current = db.get(Location, current.parent_id)
        if current is None:
            raise JobAdvertisementValidationError(
                "The location hierarchy contains a missing parent."
            )

    path.reverse()
    return path


def _build_snapshots(
    db: Session,
    advertisement: JobAdvertisement,
) -> None:
    """Validate taxonomy and location references and build publication snapshots."""
    if advertisement.main_category_id is None:
        raise JobAdvertisementValidationError("Select a main profession before publishing.")

    try:
        main_category = validate_category_active_chain(
            db,
            advertisement.main_category_id,
        )
    except TaxonomyError as exc:
        raise JobAdvertisementValidationError(str(exc)) from exc

    if main_category.parent_id is not None:
        raise JobAdvertisementValidationError("The main profession must be a root profession.")

    category_ids = _get_descendant_category_ids(db, main_category.id)

    subcategory_snapshot = None
    if advertisement.subcategory_id is not None:
        try:
            validate_category_active_chain(db, advertisement.subcategory_id)
        except TaxonomyError as exc:
            raise JobAdvertisementValidationError(str(exc)) from exc

        subcategory_path = _get_category_path(
            db,
            advertisement.subcategory_id,
        )

        if len(subcategory_path) < 2 or subcategory_path[0].id != main_category.id:
            raise JobAdvertisementValidationError(
                "The selected subcategory must belong to the main profession."
            )

        subcategory_snapshot = {
            "id": str(subcategory_path[-1].id),
            "name": subcategory_path[-1].name,
            "path": [category.name for category in subcategory_path],
        }

    selected_skills = list(advertisement.skills)
    skill_ids = {skill.id for skill in selected_skills}

    if skill_ids:
        associated_ids = set(
            db.scalars(
                select(category_skills.c.skill_id).where(
                    category_skills.c.category_id.in_(category_ids),
                    category_skills.c.skill_id.in_(skill_ids),
                )
            ).all()
        )

        if associated_ids != skill_ids:
            raise JobAdvertisementValidationError(
                "Every required skill must belong to the selected profession."
            )

        inactive_skills = [skill.name for skill in selected_skills if not skill.is_active]
        if inactive_skills:
            raise JobAdvertisementValidationError("Inactive skills cannot be selected.")

    advertisement.profession_snapshot = {
        "id": str(main_category.id),
        "name": main_category.name,
        "path": [
            category.name
            for category in _get_category_path(
                db,
                main_category.id,
            )
        ],
    }
    advertisement.subcategory_snapshot = subcategory_snapshot
    advertisement.skills_snapshot = sorted(
        ({"id": str(skill.id), "name": skill.name} for skill in selected_skills),
        key=lambda item: item["name"].casefold(),
    )

    advertisement.location_snapshot = None

    if advertisement.location_id is not None:
        try:
            validate_active_location_chain(db, advertisement.location_id)
        except LocationError as exc:
            raise JobAdvertisementValidationError(str(exc)) from exc

        location_path = _get_location_path(db, advertisement.location_id)
        advertisement.location_snapshot = {
            "id": str(location_path[-1].id),
            "name": location_path[-1].name,
            "path": [
                {
                    "id": str(location.id),
                    "name": location.name,
                    "type": location.location_type,
                }
                for location in location_path
            ],
        }


def _validate_publication(
    advertisement: JobAdvertisement,
    now: datetime,
    expires_at: datetime,
) -> None:
    """Enforce all requirements for publishing or editing a published ad."""
    if not advertisement.title or not advertisement.title.strip():
        raise JobAdvertisementValidationError("A title is required.")

    if len(advertisement.title) > 160:
        raise JobAdvertisementValidationError("The title cannot exceed 160 characters.")

    if not advertisement.description or not advertisement.description.strip():
        raise JobAdvertisementValidationError("A description is required.")

    if advertisement.job_type not in JOB_TYPES:
        raise JobAdvertisementValidationError("Select a valid job type.")

    if advertisement.work_arrangement not in WORK_ARRANGEMENTS:
        raise JobAdvertisementValidationError("Select a valid work arrangement.")

    if advertisement.main_category_id is None:
        raise JobAdvertisementValidationError("Select a main profession.")

    if advertisement.compensation_type not in COMPENSATION_TYPES:
        raise JobAdvertisementValidationError("Select a valid compensation type.")

    if (
        advertisement.work_arrangement in {"on_site", "hybrid"}
        and advertisement.location_id is None
    ):
        raise JobAdvertisementValidationError(
            "A work location is required for on-site and hybrid jobs."
        )

    if advertisement.compensation_type == "fixed":
        if advertisement.payment_unit not in PAYMENT_UNITS:
            raise JobAdvertisementValidationError("Select a payment unit for fixed compensation.")

        if advertisement.min_compensation is None and advertisement.max_compensation is None:
            raise JobAdvertisementValidationError(
                "Provide at least a minimum or maximum compensation amount."
            )
    else:
        if advertisement.min_compensation is not None or advertisement.max_compensation is not None:
            raise JobAdvertisementValidationError(
                "Negotiable and undisclosed compensation cannot include amounts."
            )

    if (
        advertisement.min_compensation is not None
        and advertisement.max_compensation is not None
        and advertisement.max_compensation < advertisement.min_compensation
    ):
        raise JobAdvertisementValidationError(
            "Maximum compensation cannot be less than minimum compensation."
        )

    if (
        isinstance(advertisement.workers_needed, bool)
        or advertisement.workers_needed is None
        or advertisement.workers_needed < 1
    ):
        raise JobAdvertisementValidationError("At least one worker must be required.")

    if not advertisement.contact_methods:
        raise JobAdvertisementValidationError("Select at least one contact method.")

    if advertisement.positions_filled < 0:
        raise JobAdvertisementValidationError("Filled positions cannot be negative.")

    if advertisement.positions_filled > advertisement.workers_needed:
        raise JobAdvertisementValidationError(
            "Filled positions cannot exceed the number of workers needed."
        )

    if advertisement.start_date is not None:
        if advertisement.start_date < _today_in_kenya():
            raise JobAdvertisementValidationError("The start date cannot be in the past.")
        if advertisement.start_date > expires_at.astimezone(KENYA_TIMEZONE).date():
            raise JobAdvertisementValidationError(
                "The start date cannot be later than advertisement expiry."
            )

    if advertisement.response_deadline is not None:
        deadline = advertisement.response_deadline
        if deadline <= now:
            raise JobAdvertisementValidationError("The response deadline must be in the future.")
        if deadline > expires_at:
            raise JobAdvertisementValidationError(
                "The response deadline cannot be later than advertisement expiry."
            )

    if expires_at <= now:
        raise JobAdvertisementValidationError("Advertisement expiry must be in the future.")


def _ensure_not_lapsed(advertisement: JobAdvertisement) -> None:
    """Reject employer changes once an advertisement's expiry time has passed."""
    if advertisement.expires_at is not None and advertisement.expires_at <= _now():
        raise JobAdvertisementConflictError("This advertisement has expired.")


def _get_settings(db: Session) -> JobAdSettings:
    settings = db.get(JobAdSettings, 1)
    if settings is None:
        raise JobAdvertisementConflictError("Job advertisement settings have not been initialized.")
    return settings


def get_employer_posting_credit_balance(
    db: Session,
    employer_profile_id: UUID,
) -> int:
    """Calculate the current balance from the immutable credit ledger."""
    balance = db.scalar(
        select(func.coalesce(func.sum(EmployerPostingCreditEntry.credit_delta), 0)).where(
            EmployerPostingCreditEntry.employer_profile_id == employer_profile_id
        )
    )
    return int(balance or 0)


def create_job_advertisement_draft(
    db: Session,
    user_id: UUID,
    values: dict | None = None,
) -> JobAdvertisement:
    """Create an incomplete draft without consuming a posting credit."""
    employer = _get_active_employer_profile(db, user_id)
    normalized = _normalize_values(values or {})

    advertisement = JobAdvertisement(
        employer_profile_id=employer.id,
        status="draft",
    )
    db.add(advertisement)
    db.flush()

    _apply_values(advertisement, normalized)

    if "skill_ids" in normalized:
        skills = db.scalars(select(Skill).where(Skill.id.in_(normalized["skill_ids"]))).all()
        if len(skills) != len(normalized["skill_ids"]):
            raise JobAdvertisementValidationError("One or more selected skills do not exist.")
        advertisement.skills = skills

    if "contact_methods" in normalized:
        _replace_contact_methods(
            advertisement,
            normalized["contact_methods"],
        )

    db.flush()
    return advertisement


def update_job_advertisement(
    db: Session,
    user_id: UUID,
    advertisement_id: UUID,
    values: dict,
) -> JobAdvertisement:
    """Update an employer's draft or published advertisement."""
    employer = _get_active_employer_profile(db, user_id)
    advertisement = _get_owned_advertisement(
        db,
        employer.id,
        advertisement_id,
    )

    if advertisement.status not in {"draft", "published"}:
        raise JobAdvertisementConflictError("This advertisement can no longer be edited.")

    _ensure_not_lapsed(advertisement)

    normalized = _normalize_values(values)

    if advertisement.status == "published":
        lock_taxonomy_mutations(db)
        lock_location_mutations(db)

    previous_values = {}
    for field in normalized:
        if field in MUTABLE_FIELDS:
            previous = getattr(advertisement, field)
            previous_values[field] = (
                previous.isoformat()
                if isinstance(previous, (date, datetime))
                else str(previous)
                if isinstance(previous, Decimal)
                else str(previous)
                if isinstance(previous, UUID)
                else previous
            )

    _apply_values(advertisement, normalized)

    if "skill_ids" in normalized:
        skills = db.scalars(select(Skill).where(Skill.id.in_(normalized["skill_ids"]))).all()
        if len(skills) != len(normalized["skill_ids"]):
            raise JobAdvertisementValidationError("One or more selected skills do not exist.")
        advertisement.skills = skills

    if "contact_methods" in normalized:
        _replace_contact_methods(
            advertisement,
            normalized["contact_methods"],
        )

    if advertisement.status == "published":
        now = _now()
        _validate_publication(
            advertisement,
            now,
            advertisement.expires_at,
        )
        _build_snapshots(db, advertisement)

    changes = {}
    for field, old_value in previous_values.items():
        current = getattr(advertisement, field)
        current_value = (
            current.isoformat()
            if isinstance(current, (date, datetime))
            else str(current)
            if isinstance(current, Decimal)
            else str(current)
            if isinstance(current, UUID)
            else current
        )
        if current_value != old_value:
            changes[field] = {
                "before": old_value,
                "after": current_value,
            }

    if "skill_ids" in normalized:
        changes["skill_ids"] = {
            "after": [str(skill.id) for skill in advertisement.skills],
        }

    if "contact_methods" in normalized:
        changes["contact_methods"] = {
            "after": sorted(method.contact_method for method in advertisement.contact_methods),
        }

    if changes:
        db.add(
            JobAdvertisementAudit(
                advertisement_id=advertisement.id,
                actor_user_id=user_id,
                action="employer_edit",
                changes=changes,
            )
        )

    db.flush()
    return advertisement


def publish_job_advertisement(
    db: Session,
    user_id: UUID,
    advertisement_id: UUID,
) -> JobAdvertisement:
    """Publish a draft and consume one credit atomically."""
    employer = _get_active_employer_profile(db, user_id)
    advertisement = _get_owned_advertisement(
        db,
        employer.id,
        advertisement_id,
    )

    if advertisement.status != "draft":
        raise JobAdvertisementConflictError("Only draft advertisements can be published.")

    lock_taxonomy_mutations(db)
    lock_location_mutations(db)

    settings = _get_settings(db)
    now = _now()
    expires_at = now + timedelta(days=settings.advertisement_duration_days)

    _validate_publication(advertisement, now, expires_at)
    _build_snapshots(db, advertisement)

    balance = get_employer_posting_credit_balance(db, employer.id)
    if balance < 1:
        raise InsufficientPostingCreditsError(
            "There are no available posting credits. Purchase a posting credit "
            "before publishing another advertisement."
        )

    db.add(
        EmployerPostingCreditEntry(
            employer_profile_id=employer.id,
            entry_type="consumption",
            credit_delta=-1,
            advertisement_id=advertisement.id,
        )
    )

    advertisement.status = "published"
    advertisement.published_at = now
    advertisement.expires_at = expires_at
    advertisement.closed_at = None

    db.add(
        JobAdvertisementAudit(
            advertisement_id=advertisement.id,
            actor_user_id=user_id,
            action="publish",
            changes={
                "published_at": now.isoformat(),
                "expires_at": expires_at.isoformat(),
            },
        )
    )

    db.flush()
    return advertisement


def close_job_advertisement(
    db: Session,
    user_id: UUID,
    advertisement_id: UUID,
) -> JobAdvertisement:
    """Close an employer's published advertisement without restoring its credit."""
    employer = _get_active_employer_profile(db, user_id)
    advertisement = _get_owned_advertisement(
        db,
        employer.id,
        advertisement_id,
    )

    if advertisement.status != "published":
        raise JobAdvertisementConflictError("Only published advertisements can be closed.")

    _ensure_not_lapsed(advertisement)

    now = _now()
    advertisement.status = "closed"
    advertisement.closed_at = now

    db.add(
        JobAdvertisementAudit(
            advertisement_id=advertisement.id,
            actor_user_id=user_id,
            action="employer_close",
            changes={"closed_at": now.isoformat()},
        )
    )

    db.flush()
    return advertisement


def update_job_advertisement_positions_filled(
    db: Session,
    user_id: UUID,
    advertisement_id: UUID,
    positions_filled: int,
) -> JobAdvertisement:
    """Update the employer-managed number of filled positions."""
    if (
        isinstance(positions_filled, bool)
        or not isinstance(positions_filled, int)
        or positions_filled < 0
    ):
        raise JobAdvertisementValidationError(
            "Filled positions must be a non-negative whole number."
        )

    employer = _get_active_employer_profile(db, user_id)
    advertisement = _get_owned_advertisement(
        db,
        employer.id,
        advertisement_id,
    )

    if advertisement.status != "published":
        raise JobAdvertisementConflictError(
            "Positions can only be updated on published advertisements."
        )

    _ensure_not_lapsed(advertisement)

    if positions_filled > advertisement.workers_needed:
        raise JobAdvertisementValidationError(
            "Filled positions cannot exceed the number of workers needed."
        )

    previous_value = advertisement.positions_filled
    advertisement.positions_filled = positions_filled

    if previous_value != positions_filled:
        db.add(
            JobAdvertisementAudit(
                advertisement_id=advertisement.id,
                actor_user_id=user_id,
                action="employer_edit",
                changes={
                    "positions_filled": {
                        "before": previous_value,
                        "after": positions_filled,
                    }
                },
            )
        )

    db.flush()
    return advertisement


def list_employer_job_advertisements(
    db: Session,
    user_id: UUID,
    *,
    page: int = 1,
    page_size: int = 20,
    status_filter: str | None = None,
) -> tuple[list[JobAdvertisement], int]:
    """List advertisements belonging to the authenticated employer."""
    if isinstance(page, bool) or not isinstance(page, int) or page < 1:
        raise JobAdvertisementValidationError("Page must be a positive whole number.")

    if isinstance(page_size, bool) or not isinstance(page_size, int) or not 1 <= page_size <= 100:
        raise JobAdvertisementValidationError("Page size must be between 1 and 100.")

    allowed_statuses = {
        "draft",
        "published",
        "suspended",
        "expired",
        "removed",
        "closed",
    }
    if status_filter is not None and status_filter not in allowed_statuses:
        raise JobAdvertisementValidationError("Invalid advertisement status filter.")

    employer = _get_active_employer_profile(db, user_id)

    filters = [JobAdvertisement.employer_profile_id == employer.id]
    if status_filter is not None:
        filters.append(JobAdvertisement.status == status_filter)

    total = db.scalar(select(func.count(JobAdvertisement.id)).where(*filters)) or 0

    advertisements = db.scalars(
        select(JobAdvertisement)
        .where(*filters)
        .options(
            selectinload(JobAdvertisement.skills),
            selectinload(JobAdvertisement.contact_methods),
        )
        .order_by(
            JobAdvertisement.created_at.desc(),
            JobAdvertisement.id.desc(),
        )
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).all()

    return list(advertisements), int(total)


def get_employer_job_advertisement(
    db: Session,
    user_id: UUID,
    advertisement_id: UUID,
) -> JobAdvertisement:
    """Retrieve one advertisement owned by the authenticated employer."""
    employer = _get_active_employer_profile(db, user_id)

    return _get_owned_advertisement(
        db,
        employer.id,
        advertisement_id,
    )


def get_employer_posting_credit_summary(
    db: Session,
    user_id: UUID,
) -> dict:
    """Retrieve an employer's credit balance and current posting prices."""
    employer = _get_active_employer_profile(db, user_id)
    settings = _get_settings(db)

    return {
        "available_credits": get_employer_posting_credit_balance(db, employer.id),
        "initial_package_credits": settings.initial_package_credits,
        "initial_package_price": settings.initial_package_price,
        "additional_credit_price": settings.additional_credit_price,
        "currency_code": settings.currency_code,
    }


def _get_advertisement_for_moderation(
    db: Session,
    advertisement_id: UUID,
) -> JobAdvertisement:
    """Retrieve and lock an advertisement for a moderation operation."""
    advertisement = db.scalar(
        select(JobAdvertisement).where(JobAdvertisement.id == advertisement_id).with_for_update()
    )

    if advertisement is None:
        raise JobAdvertisementNotFoundError("Job advertisement not found.")

    return advertisement


def _validate_moderation_reason(reason: str) -> str:
    """Normalize and validate a moderation reason."""
    if not isinstance(reason, str) or not reason.strip():
        raise JobAdvertisementValidationError("A reason is required for this moderation action.")

    return reason.strip()


def suspend_job_advertisement(
    db: Session,
    actor_user_id: UUID,
    advertisement_id: UUID,
    reason: str,
) -> JobAdvertisement:
    """Suspend a published advertisement and record the moderation decision."""
    reason = _validate_moderation_reason(reason)
    advertisement = _get_advertisement_for_moderation(db, advertisement_id)

    if advertisement.status != "published":
        raise JobAdvertisementConflictError("Only published advertisements can be suspended.")

    previous_status = advertisement.status
    advertisement.status = "suspended"

    db.add(
        JobAdvertisementAudit(
            advertisement_id=advertisement.id,
            actor_user_id=actor_user_id,
            action="suspend",
            reason=reason,
            changes={
                "status": {
                    "before": previous_status,
                    "after": "suspended",
                }
            },
        )
    )

    db.flush()
    return advertisement


def reinstate_job_advertisement(
    db: Session,
    actor_user_id: UUID,
    advertisement_id: UUID,
    reason: str,
) -> JobAdvertisement:
    """Reinstate a suspended advertisement without extending its expiry."""
    reason = _validate_moderation_reason(reason)
    advertisement = _get_advertisement_for_moderation(db, advertisement_id)

    if advertisement.status != "suspended":
        raise JobAdvertisementConflictError("Only suspended advertisements can be reinstated.")

    now = _now()

    if advertisement.expires_at is None or advertisement.expires_at <= now:
        raise JobAdvertisementConflictError(
            "This advertisement has expired and cannot be reinstated."
        )

    previous_status = advertisement.status
    advertisement.status = "published"

    db.add(
        JobAdvertisementAudit(
            advertisement_id=advertisement.id,
            actor_user_id=actor_user_id,
            action="reinstate",
            reason=reason,
            changes={
                "status": {
                    "before": previous_status,
                    "after": "published",
                }
            },
        )
    )

    db.flush()
    return advertisement


def remove_job_advertisement(
    db: Session,
    actor_user_id: UUID,
    advertisement_id: UUID,
    reason: str,
) -> JobAdvertisement:
    """Remove an advertisement without permanently deleting its records."""
    reason = _validate_moderation_reason(reason)
    advertisement = _get_advertisement_for_moderation(db, advertisement_id)

    if advertisement.status == "removed":
        raise JobAdvertisementConflictError("This advertisement has already been removed.")

    previous_status = advertisement.status
    advertisement.status = "removed"

    db.add(
        JobAdvertisementAudit(
            advertisement_id=advertisement.id,
            actor_user_id=actor_user_id,
            action="remove",
            reason=reason,
            changes={
                "status": {
                    "before": previous_status,
                    "after": "removed",
                }
            },
        )
    )

    db.flush()
    return advertisement


def expire_due_job_advertisements(
    db: Session,
    *,
    batch_size: int = 100,
    now: datetime | None = None,
) -> int:
    """Expire one batch of published advertisements whose expiry has passed.

    Only published advertisements are expired. Suspended advertisements keep
    their moderation status. Consumed credits are never restored. Rows are
    locked with SKIP LOCKED so overlapping runs cannot process the same row.
    Returns the number of advertisements expired in this batch.
    """
    if isinstance(batch_size, bool) or not isinstance(batch_size, int) or batch_size < 1:
        raise JobAdvertisementValidationError("Batch size must be a positive whole number.")

    current_time = now or _now()

    due = db.scalars(
        select(JobAdvertisement)
        .where(
            JobAdvertisement.status == "published",
            JobAdvertisement.expires_at <= current_time,
        )
        .order_by(JobAdvertisement.expires_at, JobAdvertisement.id)
        .limit(batch_size)
        .with_for_update(skip_locked=True)
    ).all()

    for advertisement in due:
        advertisement.status = "expired"
        db.add(
            JobAdvertisementAudit(
                advertisement_id=advertisement.id,
                actor_user_id=None,
                action="expire",
                changes={
                    "status": {"before": "published", "after": "expired"},
                    "expires_at": advertisement.expires_at.isoformat(),
                },
            )
        )

    db.flush()
    return len(due)
