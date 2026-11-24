from decimal import Decimal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.identity import UserRole
from app.models.job_advertisement import JobAdSettingChange, JobAdSettings

PRICE_LIMITS = (1, 100_000)
CREDIT_LIMITS = (1, 100)

LIMITS = {
    "initial_package_price": PRICE_LIMITS,
    "initial_package_credits": CREDIT_LIMITS,
    "additional_credit_price": PRICE_LIMITS,
}

CREDIT_SETTINGS = {"initial_package_credits"}
SOURCES = {"cli", "admin"}


class JobAdSettingsError(Exception):
    """Base class for job advertisement settings failures."""


class JobAdSettingsValidationError(JobAdSettingsError):
    """Raised when a requested setting value is not acceptable."""


class JobAdSettingsNotFoundError(JobAdSettingsError):
    """Raised when the settings row has not been initialized."""


def _validate(name: str, value: object) -> int:
    low, high = LIMITS[name]

    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise JobAdSettingsValidationError(f"{name} must be a whole number from {low} to {high}.")

    return value


def _validate_admin_actor(db: Session, actor_id: UUID | None) -> None:
    """Require and lock the administrator role belonging to the recorded actor."""
    if actor_id is None:
        raise JobAdSettingsValidationError(
            "An administrator actor is required for admin-originated changes."
        )

    admin_role_id = db.scalar(
        select(UserRole.id)
        .where(
            UserRole.user_id == actor_id,
            UserRole.role == "admin",
        )
        .with_for_update()
        .limit(1)
    )

    if admin_role_id is None:
        raise JobAdSettingsValidationError(
            "The recorded actor does not have the administrator role."
        )


def update_job_ad_settings(
    db: Session,
    *,
    source: str,
    changed_by_user_id: UUID | None = None,
    initial_package_price: int | None = None,
    initial_package_credits: int | None = None,
    additional_credit_price: int | None = None,
) -> list[JobAdSettingChange]:
    """Apply setting changes and record one history row per changed value.

    Every requested value is validated before anything is written.
    Administrator-originated changes require an existing administrator actor.

    The settings row is locked to prevent concurrent updates from overwriting
    each other. The updated_by_user_id field records the actor responsible for
    the latest change. CLI-originated changes have no user actor, so that field
    is cleared when a CLI change is made.

    Existing payments are not modified. They retain the amount and credits
    with which they were originally created.

    The caller is responsible for committing or rolling back the transaction.
    """
    if source not in SOURCES:
        raise JobAdSettingsValidationError("Unknown change source.")

    requested = {
        name: value
        for name, value in (
            ("initial_package_price", initial_package_price),
            ("initial_package_credits", initial_package_credits),
            ("additional_credit_price", additional_credit_price),
        )
        if value is not None
    }

    # Validate every requested value before performing any database writes.
    for name, value in requested.items():
        _validate(name, value)

    if source == "admin":
        _validate_admin_actor(db, changed_by_user_id)

    settings = db.scalar(
        select(JobAdSettings)
        .where(JobAdSettings.id == 1)
        .with_for_update()
        .execution_options(populate_existing=True)
    )

    if settings is None:
        raise JobAdSettingsNotFoundError("Job advertisement settings have not been initialized.")

    changes: list[JobAdSettingChange] = []

    for name, value in requested.items():
        current = Decimal(getattr(settings, name))
        new_value = Decimal(value)

        if current == new_value:
            continue

        change = JobAdSettingChange(
            setting_name=name,
            old_value=current,
            new_value=new_value,
            changed_by_user_id=changed_by_user_id,
            source=source,
        )
        db.add(change)

        setattr(
            settings,
            name,
            value if name in CREDIT_SETTINGS else new_value,
        )
        changes.append(change)

    if changes:
        # Keep the singleton metadata consistent with the latest update.
        # SQLAlchemy's onupdate configuration updates updated_at when this
        # settings row is persisted.
        settings.updated_by_user_id = changed_by_user_id if source == "admin" else None

    db.flush()
    return changes
