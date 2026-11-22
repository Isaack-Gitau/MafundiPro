from collections.abc import Generator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.database.session import engine
from app.models.employer import EmployerProfile
from app.models.identity import User
from app.models.job_advertisement import (
    EmployerPostingCreditEntry,
    Payment,
    PaymentRefund,
)
from app.services.employers import create_employer_profile
from app.services.job_advertisements import (
    InsufficientPostingCreditsError,
    create_job_advertisement_draft,
    get_employer_posting_credit_balance,
    publish_job_advertisement,
)
from app.services.payment_refunds import (
    PaymentRefundConflictError,
    PaymentRefundNotFoundError,
    PaymentRefundValidationError,
    refund_payment,
)
from app.services.taxonomy import create_category


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
        full_name="Refund Test User",
        username=f"refund_{unique_id}",
        username_key=f"refund_{unique_id}",
        phone_number=f"+2547{uuid4().int % 100_000_000:08d}",
        password_hash="test-password-hash",
        account_status="active",
        phone_verified_at=datetime.now(UTC),
    )
    db.add(user)
    db.flush()
    employer = create_employer_profile(db, user.id, employer_type="individual")
    return user, employer


def grant_credits(
    db: Session,
    user: User,
    employer: EmployerProfile,
    credits: int,
    *,
    amount: str = "50.00",
    entry_type: str = "initial_grant",
    age_days: int = 0,
) -> Payment:
    purpose = (
        "initial_employer_package" if entry_type == "initial_grant" else "additional_posting_credit"
    )
    payment = Payment(
        employer_profile_id=employer.id,
        initiated_by_user_id=user.id,
        purpose=purpose,
        provider="test",
        provider_environment="sandbox",
        idempotency_key=f"refund-test-{uuid4()}",
        amount=Decimal(amount),
        currency_code="KES",
        status="succeeded",
        verified_at=datetime.now(UTC),
    )
    db.add(payment)
    db.flush()
    entry = EmployerPostingCreditEntry(
        employer_profile_id=employer.id,
        entry_type=entry_type,
        credit_delta=credits,
        payment_id=payment.id,
        created_at=datetime.now(UTC) - timedelta(days=age_days),
    )
    db.add(entry)
    db.flush()
    return payment


def make_draft(db: Session, user: User):
    category = create_category(db, f"Construction {uuid4().hex[:8]}")
    return create_job_advertisement_draft(
        db,
        user.id,
        {
            "title": "Roof Repair Specialist",
            "description": "Experienced roofer needed for residential roof repairs.",
            "job_type": "casual_daily",
            "work_arrangement": "remote",
            "main_category_id": category.id,
            "compensation_type": "negotiable",
            "workers_needed": 1,
            "contact_methods": ["in_app_message"],
        },
    )


def publish_one(db: Session, user: User):
    draft = make_draft(db, user)
    return publish_job_advertisement(db, user.id, draft.id)


def consumption_source(db: Session, advertisement_id):
    entry = (
        db.query(EmployerPostingCreditEntry)
        .filter_by(entry_type="consumption", advertisement_id=advertisement_id)
        .one()
    )
    return entry.source_payment_id


def test_publish_consumes_oldest_payment_first(db_session: Session) -> None:
    user, employer = create_test_employer(db_session)
    older = grant_credits(db_session, user, employer, 5, age_days=2)
    grant_credits(db_session, user, employer, 1, entry_type="additional_purchase", age_days=1)

    published = publish_one(db_session, user)

    assert consumption_source(db_session, published.id) == older.id


def test_fifo_moves_to_next_payment_when_oldest_is_used_up(db_session: Session) -> None:
    user, employer = create_test_employer(db_session)
    older = grant_credits(
        db_session, user, employer, 1, entry_type="additional_purchase", age_days=2
    )
    newer = grant_credits(db_session, user, employer, 5, age_days=1)

    first = publish_one(db_session, user)
    second = publish_one(db_session, user)

    assert consumption_source(db_session, first.id) == older.id
    assert consumption_source(db_session, second.id) == newer.id


def test_refund_partially_used_initial_package(db_session: Session) -> None:
    user, employer = create_test_employer(db_session)
    package = grant_credits(db_session, user, employer, 5, amount="50.00")
    publish_one(db_session, user)
    publish_one(db_session, user)
    admin, _ = create_test_employer(db_session)

    refund = refund_payment(db_session, admin.id, package.id, "Customer request.")

    assert refund.credits_refunded == 3
    assert refund.amount == Decimal("30.00")
    assert refund.payout_status == "recorded"
    assert refund.refunded_by_user_id == admin.id
    assert get_employer_posting_credit_balance(db_session, employer.id) == 0

    reversal = (
        db_session.query(EmployerPostingCreditEntry)
        .filter_by(entry_type="refund_reversal", refund_id=refund.id)
        .one()
    )
    assert reversal.credit_delta == -3
    assert reversal.source_payment_id == package.id


def test_refund_single_credit_leaves_other_payments_untouched(db_session: Session) -> None:
    user, employer = create_test_employer(db_session)
    grant_credits(db_session, user, employer, 5, age_days=2)
    single = grant_credits(
        db_session, user, employer, 1, entry_type="additional_purchase", age_days=1
    )
    publish_one(db_session, user)
    admin, _ = create_test_employer(db_session)

    refund = refund_payment(db_session, admin.id, single.id, "Duplicate purchase.")

    assert refund.credits_refunded == 1
    assert refund.amount == Decimal("50.00")
    assert get_employer_posting_credit_balance(db_session, employer.id) == 4


def test_refunded_credits_cannot_be_spent(db_session: Session) -> None:
    user, employer = create_test_employer(db_session)
    package = grant_credits(db_session, user, employer, 5)
    draft = make_draft(db_session, user)
    admin, _ = create_test_employer(db_session)

    refund_payment(db_session, admin.id, package.id, "Customer request.")

    with pytest.raises(InsufficientPostingCreditsError):
        publish_job_advertisement(db_session, user.id, draft.id)
    db_session.refresh(draft)
    assert draft.status == "draft"


def test_payment_cannot_be_refunded_twice(db_session: Session) -> None:
    user, employer = create_test_employer(db_session)
    package = grant_credits(db_session, user, employer, 5)
    admin, _ = create_test_employer(db_session)
    refund_payment(db_session, admin.id, package.id, "First.")

    with pytest.raises(PaymentRefundConflictError, match="already been refunded"):
        refund_payment(db_session, admin.id, package.id, "Second.")

    assert db_session.query(PaymentRefund).filter_by(payment_id=package.id).count() == 1


def test_cannot_refund_when_all_credits_were_used(db_session: Session) -> None:
    user, employer = create_test_employer(db_session)
    single = grant_credits(db_session, user, employer, 1, entry_type="additional_purchase")
    publish_one(db_session, user)
    admin, _ = create_test_employer(db_session)

    with pytest.raises(PaymentRefundConflictError, match="have been used"):
        refund_payment(db_session, admin.id, single.id, "Too late.")

    assert db_session.query(PaymentRefund).count() == 0


def test_refund_requires_reason(db_session: Session) -> None:
    user, employer = create_test_employer(db_session)
    package = grant_credits(db_session, user, employer, 5)

    with pytest.raises(PaymentRefundValidationError):
        refund_payment(db_session, user.id, package.id, "   ")


def test_only_succeeded_payments_can_be_refunded(db_session: Session) -> None:
    user, employer = create_test_employer(db_session)
    payment = Payment(
        employer_profile_id=employer.id,
        initiated_by_user_id=user.id,
        purpose="additional_posting_credit",
        provider="test",
        provider_environment="sandbox",
        idempotency_key=f"refund-test-{uuid4()}",
        amount=Decimal("50.00"),
        currency_code="KES",
        status="pending",
    )
    db_session.add(payment)
    db_session.flush()

    with pytest.raises(PaymentRefundConflictError, match="succeeded"):
        refund_payment(db_session, user.id, payment.id, "Not paid.")


def test_refund_unknown_payment_is_not_found(db_session: Session) -> None:
    user, _ = create_test_employer(db_session)

    with pytest.raises(PaymentRefundNotFoundError):
        refund_payment(db_session, user.id, uuid4(), "Unknown.")


def test_database_requires_source_payment_on_consumption(db_session: Session) -> None:
    user, employer = create_test_employer(db_session)
    draft = make_draft(db_session, user)

    with pytest.raises(IntegrityError) as info:
        with db_session.begin_nested():
            db_session.add(
                EmployerPostingCreditEntry(
                    employer_profile_id=employer.id,
                    entry_type="consumption",
                    credit_delta=-1,
                    advertisement_id=draft.id,
                )
            )
            db_session.flush()

    assert info.value.orig.diag.constraint_name == "ck_employer_posting_credit_entries_integrity"
