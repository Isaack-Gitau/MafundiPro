from collections.abc import Generator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import update
from sqlalchemy.orm import Session

from app.database.session import engine
from app.models.employer import EmployerProfile
from app.models.identity import User
from app.models.job_advertisement import Payment, PaymentEvent
from app.services.employers import create_employer_profile
from app.services.job_advertisements import get_employer_posting_credit_balance
from app.services.payment_callbacks import process_mpesa_callback
from app.services.payment_providers import FakePaymentProvider, StkStatus
from app.services.payment_reconciliation import (
    find_anomalies,
    find_due_payment_ids,
    reconcile_payment,
)
from app.services.payments import INITIAL_PURPOSE, initiate_payment

RECEIPT = "RECON123ABC"


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
        full_name="Reconcile Test User",
        username=f"recon_{unique_id}",
        username_key=f"recon_{unique_id}",
        phone_number=f"+2547{uuid4().int % 100_000_000:08d}",
        password_hash="test-password-hash",
        account_status="active",
        phone_verified_at=datetime.now(UTC),
    )
    db.add(user)
    db.flush()
    employer = create_employer_profile(db, user.id, employer_type="individual")
    return user, employer


def start(db: Session, user: User, provider: FakePaymentProvider) -> Payment:
    payment, _ = initiate_payment(
        db,
        user.id,
        purpose=INITIAL_PURPOSE,
        payer_phone="0712345678",
        idempotency_key=f"key-{uuid4().hex}",
        provider=provider,
    )
    return payment


def script(provider, payment, *, code=0, amount="50", receipt=RECEIPT) -> None:
    provider.statuses[payment.provider_reference] = StkStatus(
        result_code=code, amount=Decimal(amount), receipt=receipt
    )


def test_reconciliation_grants_a_confirmed_payment(db_session: Session) -> None:
    user, employer = create_test_employer(db_session)
    provider = FakePaymentProvider()
    payment = start(db_session, user, provider)
    script(provider, payment)

    outcome = reconcile_payment(db_session, provider, payment.id)

    assert outcome == "granted"
    assert payment.status == "succeeded"
    assert payment.mpesa_receipt_number == RECEIPT
    assert payment.verified_at is not None
    assert get_employer_posting_credit_balance(db_session, employer.id) == 5
    event = db_session.query(PaymentEvent).filter_by(payment_id=payment.id).one()
    assert (event.event_type, event.outcome) == ("status_query", "granted")


def test_unavailable_status_leaves_payment_pending(db_session: Session) -> None:
    user, employer = create_test_employer(db_session)
    provider = FakePaymentProvider()
    payment = start(db_session, user, provider)

    outcome = reconcile_payment(db_session, provider, payment.id)

    assert outcome == "verification_unavailable"
    assert payment.status == "pending"
    assert get_employer_posting_credit_balance(db_session, employer.id) == 0


@pytest.mark.parametrize(("code", "expected"), [(1, "failed"), (1032, "cancelled")])
def test_unsuccessful_status_closes_payment(db_session: Session, code: int, expected: str) -> None:
    user, employer = create_test_employer(db_session)
    provider = FakePaymentProvider()
    payment = start(db_session, user, provider)
    script(provider, payment, code=code)

    outcome = reconcile_payment(db_session, provider, payment.id)

    assert outcome == "payment_failed"
    assert payment.status == expected
    assert get_employer_posting_credit_balance(db_session, employer.id) == 0


def test_wrong_amount_is_rejected(db_session: Session) -> None:
    user, employer = create_test_employer(db_session)
    provider = FakePaymentProvider()
    payment = start(db_session, user, provider)
    script(provider, payment, amount="10")

    outcome = reconcile_payment(db_session, provider, payment.id)

    assert outcome == "rejected_invalid_success"
    assert payment.status == "pending"
    assert get_employer_posting_credit_balance(db_session, employer.id) == 0


def test_already_settled_payment_is_skipped_and_not_granted_twice(db_session: Session) -> None:
    user, employer = create_test_employer(db_session)
    provider = FakePaymentProvider()
    payment = start(db_session, user, provider)
    script(provider, payment)

    first = reconcile_payment(db_session, provider, payment.id)
    second = reconcile_payment(db_session, provider, payment.id)

    assert (first, second) == ("granted", "skipped")
    assert get_employer_posting_credit_balance(db_session, employer.id) == 5


def test_receipt_already_used_by_another_payment_is_rejected(db_session: Session) -> None:
    first_user, _ = create_test_employer(db_session)
    second_user, second_employer = create_test_employer(db_session)
    provider = FakePaymentProvider()
    first = start(db_session, first_user, provider)
    second = start(db_session, second_user, provider)
    script(provider, first)
    script(provider, second)
    reconcile_payment(db_session, provider, first.id)

    outcome = reconcile_payment(db_session, provider, second.id)

    assert outcome == "duplicate_receipt"
    assert second.status == "pending"
    assert get_employer_posting_credit_balance(db_session, second_employer.id) == 0


def test_callback_and_reconciliation_do_not_double_grant(db_session: Session) -> None:
    user, employer = create_test_employer(db_session)
    provider = FakePaymentProvider()
    payment = start(db_session, user, provider)
    script(provider, payment)
    body = {
        "Body": {
            "stkCallback": {
                "CheckoutRequestID": payment.provider_reference,
                "ResultCode": 0,
                "CallbackMetadata": {
                    "Item": [
                        {"Name": "Amount", "Value": 50.0},
                        {"Name": "MpesaReceiptNumber", "Value": RECEIPT},
                    ]
                },
            }
        }
    }

    assert process_mpesa_callback(db_session, provider, body) == "granted"
    assert reconcile_payment(db_session, provider, payment.id) == "skipped"
    assert get_employer_posting_credit_balance(db_session, employer.id) == 5


def test_due_payments_respect_minimum_age(db_session: Session) -> None:
    user, _ = create_test_employer(db_session)
    payment = start(db_session, user, FakePaymentProvider())

    assert payment.id not in find_due_payment_ids(
        db_session, min_age=timedelta(minutes=5), limit=1000
    )
    assert payment.id in find_due_payment_ids(db_session, min_age=timedelta(0), limit=1000)


def test_clean_payment_has_no_anomalies(db_session: Session) -> None:
    user, _ = create_test_employer(db_session)
    provider = FakePaymentProvider()
    payment = start(db_session, user, provider)
    script(provider, payment)
    reconcile_payment(db_session, provider, payment.id)

    report = find_anomalies(db_session)

    assert all(payment.id not in ids for ids in report.values())


def test_anomalies_flag_inconsistent_records(db_session: Session) -> None:
    user, _ = create_test_employer(db_session)
    payment = start(db_session, user, FakePaymentProvider())
    payment.status = "succeeded"
    payment.verified_at = datetime.now(UTC)
    db_session.flush()

    report = find_anomalies(db_session)

    assert payment.id in report["succeeded_without_ledger"]
    assert payment.id in report["succeeded_without_receipt"]


def test_anomalies_flag_delayed_pending_payments(db_session: Session) -> None:
    user, _ = create_test_employer(db_session)
    payment = start(db_session, user, FakePaymentProvider())
    db_session.execute(
        update(Payment)
        .where(Payment.id == payment.id)
        .values(created_at=datetime.now(UTC) - timedelta(days=2))
    )

    report = find_anomalies(db_session)

    assert payment.id in report["delayed_pending"]


def test_anomalies_flag_grants_without_success_and_mismatches(db_session: Session) -> None:
    first_user, _ = create_test_employer(db_session)
    second_user, _ = create_test_employer(db_session)
    provider = FakePaymentProvider()
    orphaned = start(db_session, first_user, provider)
    mismatched = start(db_session, second_user, provider)
    script(provider, orphaned, receipt="RECON-A")
    script(provider, mismatched, receipt="RECON-B")
    reconcile_payment(db_session, provider, orphaned.id)
    reconcile_payment(db_session, provider, mismatched.id)

    orphaned.status = "failed"
    mismatched.credits_to_grant = 3
    db_session.flush()
    report = find_anomalies(db_session)

    assert orphaned.id in report["ledger_without_success"]
    assert mismatched.id in report["grant_mismatch"]
