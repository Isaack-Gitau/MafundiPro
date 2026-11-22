from collections.abc import Generator
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy.orm import Session

from app.core.phone import normalize_kenyan_mobile
from app.database.session import engine
from app.models.employer import EmployerProfile
from app.models.identity import User
from app.services.employers import create_employer_profile
from app.services.job_advertisements import get_employer_posting_credit_balance
from app.services.payment_providers import FakePaymentProvider
from app.services.payments import (
    ADDITIONAL_PURPOSE,
    INITIAL_PURPOSE,
    PaymentConflictError,
    PaymentNotFoundError,
    PaymentValidationError,
    get_employer_payment,
    initiate_payment,
)

PHONE = "0712345678"


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


def create_test_employer(db: Session) -> tuple[User, EmployerProfile]:
    unique_id = uuid4().hex[:12]
    user = User(
        full_name="Payment Test User",
        username=f"payment_{unique_id}",
        username_key=f"payment_{unique_id}",
        phone_number=f"+2547{uuid4().int % 100_000_000:08d}",
        password_hash="test-password-hash",
        account_status="active",
        phone_verified_at=datetime.now(UTC),
    )
    db.add(user)
    db.flush()
    employer = create_employer_profile(db, user.id, employer_type="individual")
    return user, employer


def start(db, user, provider=None, *, purpose=INITIAL_PURPOSE, key=None, phone=PHONE):
    provider = provider or FakePaymentProvider()
    return initiate_payment(
        db,
        user.id,
        purpose=purpose,
        payer_phone=phone,
        idempotency_key=key or f"key-{uuid4().hex}",
        provider=provider,
    )


@pytest.mark.parametrize(
    "raw",
    ["0712345678", "0112345678", "254712345678", "+254712345678", " 0712 345-678 "],
)
def test_phone_normalization_accepts_kenyan_mobiles(raw: str) -> None:
    assert normalize_kenyan_mobile(raw).startswith("+254")
    assert len(normalize_kenyan_mobile(raw)) == 13


@pytest.mark.parametrize("raw", ["07123", "+255712345678", "0212345678", "07123456789", "abc"])
def test_phone_normalization_rejects_invalid_numbers(raw: str) -> None:
    with pytest.raises(ValueError):
        normalize_kenyan_mobile(raw)


def test_initiate_initial_package_creates_pending_payment_without_credits(
    db_session: Session,
) -> None:
    user, employer = create_test_employer(db_session)
    provider = FakePaymentProvider()

    payment, created = start(db_session, user, provider)

    assert created is True
    assert payment.status == "pending"
    assert payment.purpose == INITIAL_PURPOSE
    assert payment.amount == 50
    assert payment.credits_to_grant == 5
    assert payment.payer_phone == "+254712345678"
    assert payment.provider == "fake"
    assert payment.provider_reference.startswith("fake-checkout-")
    assert len(provider.requests) == 1
    assert get_employer_posting_credit_balance(db_session, employer.id) == 0


def test_initiate_additional_credit_uses_single_credit_price(db_session: Session) -> None:
    user, _ = create_test_employer(db_session)

    payment, _ = start(db_session, user, purpose=ADDITIONAL_PURPOSE)

    assert payment.amount == 50
    assert payment.credits_to_grant == 1


def test_idempotent_replay_returns_original_without_calling_provider_again(
    db_session: Session,
) -> None:
    user, _ = create_test_employer(db_session)
    provider = FakePaymentProvider()

    first, created_first = start(db_session, user, provider, key="replay-key-1")
    second, created_second = start(db_session, user, provider, key="replay-key-1")

    assert created_first is True
    assert created_second is False
    assert second.id == first.id
    assert len(provider.requests) == 1


def test_idempotency_key_cannot_be_reused_for_a_different_request(db_session: Session) -> None:
    user, _ = create_test_employer(db_session)
    start(db_session, user, key="shared-key-1")

    with pytest.raises(PaymentConflictError, match="idempotency key"):
        start(db_session, user, purpose=ADDITIONAL_PURPOSE, key="shared-key-1")


def test_idempotency_keys_are_scoped_per_employer(db_session: Session) -> None:
    first_user, _ = create_test_employer(db_session)
    second_user, _ = create_test_employer(db_session)

    first, _ = start(db_session, first_user, key="same-key-1")
    second, created = start(db_session, second_user, key="same-key-1")

    assert created is True
    assert second.id != first.id


def test_second_pending_payment_for_same_purpose_is_rejected(db_session: Session) -> None:
    user, _ = create_test_employer(db_session)
    start(db_session, user, purpose=ADDITIONAL_PURPOSE)

    with pytest.raises(PaymentConflictError, match="awaiting confirmation"):
        start(db_session, user, purpose=ADDITIONAL_PURPOSE)


def test_initial_package_cannot_be_bought_again_after_success(db_session: Session) -> None:
    user, _ = create_test_employer(db_session)
    payment, _ = start(db_session, user)
    payment.status = "succeeded"
    payment.verified_at = datetime.now(UTC)
    db_session.flush()

    with pytest.raises(PaymentConflictError, match="initial package"):
        start(db_session, user)


def test_failed_provider_call_marks_payment_failed_and_allows_retry(db_session: Session) -> None:
    user, _ = create_test_employer(db_session)

    failed, created = start(db_session, user, FakePaymentProvider(fail=True))

    assert created is True
    assert failed.status == "failed"
    assert failed.provider_reference is None

    retry, retry_created = start(db_session, user)
    assert retry_created is True
    assert retry.status == "pending"
    assert retry.id != failed.id


def test_unverified_account_cannot_start_payment(db_session: Session) -> None:
    user, _ = create_test_employer(db_session)
    user.phone_verified_at = None
    db_session.flush()

    with pytest.raises(PaymentValidationError, match="Verify your phone"):
        start(db_session, user)


def test_user_without_employer_profile_cannot_start_payment(db_session: Session) -> None:
    unique_id = uuid4().hex[:12]
    user = User(
        full_name="No Employer",
        username=f"noemp_{unique_id}",
        username_key=f"noemp_{unique_id}",
        phone_number=f"+2547{uuid4().int % 100_000_000:08d}",
        password_hash="test-password-hash",
        account_status="active",
        phone_verified_at=datetime.now(UTC),
    )
    db_session.add(user)
    db_session.flush()

    with pytest.raises(PaymentNotFoundError):
        start(db_session, user)


def test_invalid_purpose_is_rejected(db_session: Session) -> None:
    user, _ = create_test_employer(db_session)

    with pytest.raises(PaymentValidationError, match="purpose"):
        start(db_session, user, purpose="gift")


@pytest.mark.parametrize("key", ["", "short", "x" * 65])
def test_idempotency_key_length_is_enforced(db_session: Session, key: str) -> None:
    user, _ = create_test_employer(db_session)

    with pytest.raises(PaymentValidationError, match="idempotency key"):
        initiate_payment(
            db_session,
            user.id,
            purpose=INITIAL_PURPOSE,
            payer_phone=PHONE,
            idempotency_key=key,
            provider=FakePaymentProvider(),
        )


def test_employer_can_read_only_own_payment(db_session: Session) -> None:
    owner, _ = create_test_employer(db_session)
    other, _ = create_test_employer(db_session)
    payment, _ = start(db_session, owner)

    assert get_employer_payment(db_session, owner.id, payment.id).id == payment.id
    with pytest.raises(PaymentNotFoundError):
        get_employer_payment(db_session, other.id, payment.id)
