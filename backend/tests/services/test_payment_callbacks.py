from collections.abc import Generator
from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy.orm import Session

from app.database.session import engine
from app.models.employer import EmployerProfile
from app.models.identity import User
from app.models.job_advertisement import EmployerPostingCreditEntry, Payment, PaymentEvent
from app.services.employers import create_employer_profile
from app.services.job_advertisements import get_employer_posting_credit_balance
from app.services.payment_callbacks import process_mpesa_callback
from app.services.payment_providers import FakePaymentProvider, StkStatus
from app.services.payments import ADDITIONAL_PURPOSE, INITIAL_PURPOSE, initiate_payment

RECEIPT = "RCPT123ABC"


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
        full_name="Callback Test User",
        username=f"callback_{unique_id}",
        username_key=f"callback_{unique_id}",
        phone_number=f"+2547{uuid4().int % 100_000_000:08d}",
        password_hash="test-password-hash",
        account_status="active",
        phone_verified_at=datetime.now(UTC),
    )
    db.add(user)
    db.flush()
    employer = create_employer_profile(db, user.id, employer_type="individual")
    return user, employer


def start(db, user, provider, purpose=INITIAL_PURPOSE) -> Payment:
    payment, _ = initiate_payment(
        db,
        user.id,
        purpose=purpose,
        payer_phone="0712345678",
        idempotency_key=f"key-{uuid4().hex}",
        provider=provider,
    )
    return payment


def payload(checkout, *, code=0, amount="50", receipt=RECEIPT, phone="254712345678"):
    body = {
        "MerchantRequestID": "merchant-1",
        "CheckoutRequestID": checkout,
        "ResultCode": code,
        "ResultDesc": "result",
    }
    if code == 0:
        body["CallbackMetadata"] = {
            "Item": [
                {"Name": "Amount", "Value": float(amount)},
                {"Name": "MpesaReceiptNumber", "Value": receipt},
                {"Name": "PhoneNumber", "Value": int(phone)},
            ]
        }
    return {"Body": {"stkCallback": body}}


def confirm(provider, payment, amount="50", receipt=RECEIPT, code=0) -> None:
    provider.statuses[payment.provider_reference] = StkStatus(
        result_code=code,
        amount=Decimal(amount),
        receipt=receipt,
    )


def ledger_entries(db, employer):
    return db.query(EmployerPostingCreditEntry).filter_by(employer_profile_id=employer.id).all()


def test_verified_callback_grants_initial_package(db_session: Session) -> None:
    user, employer = create_test_employer(db_session)
    provider = FakePaymentProvider()
    payment = start(db_session, user, provider)
    confirm(provider, payment)

    outcome = process_mpesa_callback(db_session, provider, payload(payment.provider_reference))

    assert outcome == "granted"
    assert payment.status == "succeeded"
    assert payment.mpesa_receipt_number == RECEIPT
    assert payment.verified_at is not None
    assert payment.callback_received_at is not None
    assert get_employer_posting_credit_balance(db_session, employer.id) == 5
    entries = ledger_entries(db_session, employer)
    assert [(e.entry_type, e.credit_delta, e.payment_id) for e in entries] == [
        ("initial_grant", 5, payment.id)
    ]
    kinds = sorted(
        e.event_type for e in db_session.query(PaymentEvent).filter_by(payment_id=payment.id)
    )
    assert kinds == ["callback", "status_query"]


def test_verified_callback_grants_single_additional_credit(db_session: Session) -> None:
    user, employer = create_test_employer(db_session)
    provider = FakePaymentProvider()
    payment = start(db_session, user, provider, ADDITIONAL_PURPOSE)
    confirm(provider, payment)

    outcome = process_mpesa_callback(db_session, provider, payload(payment.provider_reference))

    assert outcome == "granted"
    entries = ledger_entries(db_session, employer)
    assert [(e.entry_type, e.credit_delta) for e in entries] == [("additional_purchase", 1)]


def test_replayed_callback_does_not_grant_twice(db_session: Session) -> None:
    user, employer = create_test_employer(db_session)
    provider = FakePaymentProvider()
    payment = start(db_session, user, provider)
    confirm(provider, payment)
    body = payload(payment.provider_reference)

    first = process_mpesa_callback(db_session, provider, body)
    second = process_mpesa_callback(db_session, provider, body)

    assert (first, second) == ("granted", "duplicate")
    assert get_employer_posting_credit_balance(db_session, employer.id) == 5
    assert len(ledger_entries(db_session, employer)) == 1


def test_callback_with_wrong_amount_is_rejected(db_session: Session) -> None:
    user, employer = create_test_employer(db_session)
    provider = FakePaymentProvider()
    payment = start(db_session, user, provider)
    confirm(provider, payment)

    outcome = process_mpesa_callback(
        db_session, provider, payload(payment.provider_reference, amount="10")
    )

    assert outcome == "rejected_invalid_success"
    assert payment.status == "pending"
    assert get_employer_posting_credit_balance(db_session, employer.id) == 0
    assert provider.queried == []


def test_status_query_disagreement_blocks_credits(db_session: Session) -> None:
    user, employer = create_test_employer(db_session)
    provider = FakePaymentProvider()
    payment = start(db_session, user, provider)
    confirm(provider, payment, amount="10")

    outcome = process_mpesa_callback(db_session, provider, payload(payment.provider_reference))

    assert outcome == "verification_failed"
    assert payment.status == "pending"
    assert get_employer_posting_credit_balance(db_session, employer.id) == 0


def test_unavailable_status_query_leaves_payment_pending(db_session: Session) -> None:
    user, employer = create_test_employer(db_session)
    provider = FakePaymentProvider()
    payment = start(db_session, user, provider)

    outcome = process_mpesa_callback(db_session, provider, payload(payment.provider_reference))

    assert outcome == "verification_unavailable"
    assert payment.status == "pending"
    assert get_employer_posting_credit_balance(db_session, employer.id) == 0


@pytest.mark.parametrize(("code", "expected"), [(1, "failed"), (1032, "cancelled")])
def test_unsuccessful_callback_closes_payment_without_credits(
    db_session: Session, code: int, expected: str
) -> None:
    user, employer = create_test_employer(db_session)
    provider = FakePaymentProvider()
    payment = start(db_session, user, provider)

    outcome = process_mpesa_callback(
        db_session, provider, payload(payment.provider_reference, code=code)
    )

    assert outcome == "payment_failed"
    assert payment.status == expected
    assert get_employer_posting_credit_balance(db_session, employer.id) == 0


def test_late_success_after_failure_is_honoured_once_verified(db_session: Session) -> None:
    user, employer = create_test_employer(db_session)
    provider = FakePaymentProvider()
    payment = start(db_session, user, provider)
    process_mpesa_callback(db_session, provider, payload(payment.provider_reference, code=1))
    assert payment.status == "failed"
    confirm(provider, payment)

    outcome = process_mpesa_callback(db_session, provider, payload(payment.provider_reference))

    assert outcome == "granted"
    assert payment.status == "succeeded"
    assert get_employer_posting_credit_balance(db_session, employer.id) == 5


def test_late_success_conflicting_with_new_initial_purchase_needs_review(
    db_session: Session,
) -> None:
    user, employer = create_test_employer(db_session)
    provider = FakePaymentProvider()
    first = start(db_session, user, provider)
    process_mpesa_callback(db_session, provider, payload(first.provider_reference, code=1))
    second = start(db_session, user, provider)
    assert second.status == "pending"
    confirm(provider, first)

    outcome = process_mpesa_callback(db_session, provider, payload(first.provider_reference))

    assert outcome == "needs_review"
    db_session.refresh(first)
    assert first.status == "failed"
    assert get_employer_posting_credit_balance(db_session, employer.id) == 0


def test_unknown_checkout_is_recorded_and_ignored(db_session: Session) -> None:
    provider = FakePaymentProvider()

    outcome = process_mpesa_callback(db_session, provider, payload("fake-checkout-unknown"))

    assert outcome == "unknown_payment"
    event = db_session.query(PaymentEvent).filter_by(outcome="unknown_payment").one()
    assert event.payment_id is None


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"Body": {}},
        {"Body": {"stkCallback": {"CheckoutRequestID": "x", "ResultCode": "abc"}}},
    ],
)
def test_malformed_callbacks_are_rejected(db_session: Session, body: dict) -> None:
    outcome = process_mpesa_callback(db_session, FakePaymentProvider(), body)

    assert outcome == "malformed"


def test_receipt_number_cannot_grant_two_payments(db_session: Session) -> None:
    first_user, first_employer = create_test_employer(db_session)
    second_user, second_employer = create_test_employer(db_session)
    provider = FakePaymentProvider()
    first = start(db_session, first_user, provider)
    second = start(db_session, second_user, provider)
    confirm(provider, first)
    confirm(provider, second)
    process_mpesa_callback(db_session, provider, payload(first.provider_reference))

    outcome = process_mpesa_callback(db_session, provider, payload(second.provider_reference))

    assert outcome == "duplicate_receipt"
    assert second.status == "pending"
    assert get_employer_posting_credit_balance(db_session, second_employer.id) == 0
    assert get_employer_posting_credit_balance(db_session, first_employer.id) == 5


def test_stored_events_do_not_contain_the_payer_phone(db_session: Session) -> None:
    user, _ = create_test_employer(db_session)
    provider = FakePaymentProvider()
    payment = start(db_session, user, provider)
    confirm(provider, payment)

    process_mpesa_callback(db_session, provider, payload(payment.provider_reference))

    stored = [
        str(e.payload) for e in db_session.query(PaymentEvent).filter_by(payment_id=payment.id)
    ]
    assert stored
    assert all("254712345678" not in text for text in stored)
