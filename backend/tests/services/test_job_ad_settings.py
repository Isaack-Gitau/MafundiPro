from collections.abc import Generator
from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.database.session import engine
from app.models.identity import User, UserRole
from app.models.job_advertisement import JobAdSettingChange, JobAdSettings
from app.services.employers import create_employer_profile
from app.services.job_ad_settings import (
    JobAdSettingsValidationError,
    update_job_ad_settings,
)
from app.services.payment_providers import FakePaymentProvider, StkStatus
from app.services.payment_reconciliation import reconcile_payment
from app.services.payments import INITIAL_PURPOSE, initiate_payment


@pytest.fixture
def db_session() -> Generator[Session]:
    with engine.connect() as connection:
        transaction = connection.begin()
        session = Session(bind=connection, autoflush=False, expire_on_commit=False)
        try:
            yield session
        finally:
            session.close()
            transaction.rollback()


def create_test_user(
    db: Session,
    *,
    roles: tuple[str, ...] = (),
) -> User:
    unique_id = uuid4().hex[:12]
    user = User(
        full_name="Settings Test User",
        username=f"settings_{unique_id}",
        username_key=f"settings_{unique_id}",
        phone_number=f"+2547{uuid4().int % 100_000_000:08d}",
        password_hash="test-password-hash",
        account_status="active",
        phone_verified_at=datetime.now(UTC),
    )
    user.roles.extend(UserRole(role=role) for role in roles)

    db.add(user)
    db.flush()
    return user


def history(db: Session) -> list[JobAdSettingChange]:
    return db.query(JobAdSettingChange).order_by(JobAdSettingChange.setting_name).all()


def test_change_updates_settings_and_records_history(db_session: Session) -> None:
    admin = create_test_user(db_session, roles=("admin",))
    before = db_session.get(JobAdSettings, 1).initial_package_price

    changes = update_job_ad_settings(
        db_session,
        source="admin",
        changed_by_user_id=admin.id,
        initial_package_price=75,
    )

    settings = db_session.get(JobAdSettings, 1)
    assert settings.initial_package_price == Decimal("75")
    assert len(changes) == 1

    row = history(db_session)[0]
    assert (row.setting_name, row.old_value, row.new_value) == (
        "initial_package_price",
        before,
        Decimal("75"),
    )
    assert (row.source, row.changed_by_user_id) == ("admin", admin.id)
    assert settings.updated_by_user_id == admin.id


def test_admin_change_requires_actor(db_session: Session) -> None:
    before = db_session.get(JobAdSettings, 1).initial_package_price

    with pytest.raises(JobAdSettingsValidationError, match="actor is required"):
        update_job_ad_settings(
            db_session,
            source="admin",
            initial_package_price=75,
        )

    assert db_session.get(JobAdSettings, 1).initial_package_price == before
    assert history(db_session) == []


def test_database_rejects_admin_history_without_actor(db_session: Session) -> None:
    change = JobAdSettingChange(
        setting_name="initial_package_price",
        old_value=Decimal("50"),
        new_value=Decimal("75"),
        changed_by_user_id=None,
        source="admin",
    )

    savepoint = db_session.begin_nested()
    db_session.add(change)

    with pytest.raises(IntegrityError):
        db_session.flush()

    savepoint.rollback()
    assert history(db_session) == []


def test_admin_change_rejects_nonexistent_actor(db_session: Session) -> None:
    before = db_session.get(JobAdSettings, 1).initial_package_price

    with pytest.raises(JobAdSettingsValidationError, match="does not have"):
        update_job_ad_settings(
            db_session,
            source="admin",
            changed_by_user_id=uuid4(),
            initial_package_price=75,
        )

    assert db_session.get(JobAdSettings, 1).initial_package_price == before
    assert history(db_session) == []


@pytest.mark.parametrize("roles", [(), ("moderator",)])
def test_admin_change_rejects_non_admin_actor(
    db_session: Session,
    roles: tuple[str, ...],
) -> None:
    user = create_test_user(db_session, roles=roles)
    before = db_session.get(JobAdSettings, 1).initial_package_price

    with pytest.raises(JobAdSettingsValidationError, match="does not have"):
        update_job_ad_settings(
            db_session,
            source="admin",
            changed_by_user_id=user.id,
            initial_package_price=75,
        )

    assert db_session.get(JobAdSettings, 1).initial_package_price == before
    assert history(db_session) == []


def test_unchanged_values_write_nothing(db_session: Session) -> None:
    settings = db_session.get(JobAdSettings, 1)

    changes = update_job_ad_settings(
        db_session,
        source="cli",
        initial_package_price=int(settings.initial_package_price),
        initial_package_credits=settings.initial_package_credits,
    )

    assert changes == []
    assert history(db_session) == []


def test_credit_count_change_is_recorded(db_session: Session) -> None:
    update_job_ad_settings(db_session, source="cli", initial_package_credits=8)

    assert db_session.get(JobAdSettings, 1).initial_package_credits == 8

    row = history(db_session)[0]
    assert (row.setting_name, row.new_value) == (
        "initial_package_credits",
        Decimal("8"),
    )
    assert row.source == "cli"
    assert row.changed_by_user_id is None


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("initial_package_price", 0),
        ("initial_package_price", 100_001),
        ("initial_package_price", True),
        ("initial_package_price", 5.5),
        ("additional_credit_price", "50"),
        ("initial_package_credits", 0),
        ("initial_package_credits", 101),
    ],
)
def test_invalid_values_are_rejected(
    db_session: Session,
    field: str,
    value: object,
) -> None:
    with pytest.raises(JobAdSettingsValidationError):
        update_job_ad_settings(db_session, source="cli", **{field: value})

    assert history(db_session) == []


def test_one_bad_value_rejects_the_whole_change(db_session: Session) -> None:
    before = db_session.get(JobAdSettings, 1).initial_package_price

    with pytest.raises(JobAdSettingsValidationError):
        update_job_ad_settings(
            db_session,
            source="cli",
            initial_package_price=60,
            additional_credit_price=0,
        )

    assert db_session.get(JobAdSettings, 1).initial_package_price == before
    assert history(db_session) == []


def test_price_change_does_not_affect_a_pending_payment(db_session: Session) -> None:
    user = create_test_user(db_session)
    create_employer_profile(db_session, user.id, employer_type="individual")
    provider = FakePaymentProvider()

    payment, _ = initiate_payment(
        db_session,
        user.id,
        purpose=INITIAL_PURPOSE,
        payer_phone="0712345678",
        idempotency_key=f"key-{uuid4().hex}",
        provider=provider,
    )
    original_amount = payment.amount
    original_credits = payment.credits_to_grant

    update_job_ad_settings(
        db_session,
        source="cli",
        initial_package_price=70,
        initial_package_credits=9,
    )

    assert (payment.amount, payment.credits_to_grant) == (
        original_amount,
        original_credits,
    )

    provider.statuses[payment.provider_reference] = StkStatus(
        result_code=0,
        amount=Decimal(original_amount),
        receipt="SETTINGS123",
    )

    assert reconcile_payment(db_session, provider, payment.id) == "granted"
    assert payment.status == "succeeded"
