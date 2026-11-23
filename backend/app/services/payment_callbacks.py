from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.employer import EmployerProfile
from app.models.job_advertisement import EmployerPostingCreditEntry, Payment, PaymentEvent
from app.services.payment_providers import (
    PaymentProvider,
    PaymentProviderError,
    parse_stk_callback,
)

CANCELLED_BY_USER = 1032


def _redact(payload: Any) -> Any:
    """Copy a payload with payer phone numbers masked before it is stored."""
    if isinstance(payload, dict):
        if payload.get("Name") == "PhoneNumber" and "Value" in payload:
            return {**payload, "Value": "[redacted]"}
        return {key: _redact(value) for key, value in payload.items()}
    if isinstance(payload, list):
        return [_redact(value) for value in payload]
    return payload


def _record(
    db: Session,
    provider: PaymentProvider,
    event_type: str,
    outcome: str,
    payload: Any,
    payment_id: UUID | None = None,
) -> None:
    db.add(
        PaymentEvent(
            payment_id=payment_id,
            provider=provider.name,
            event_type=event_type,
            outcome=outcome,
            payload=_redact(payload),
        )
    )
    db.flush()


def process_mpesa_callback(db: Session, provider: PaymentProvider, payload: Any) -> str:
    """Verify a provider callback and grant credits only when every check passes.

    Returns an outcome label. A callback alone never grants credits: the amount,
    the receipt and an independent status query must all agree first.
    """
    try:
        callback = parse_stk_callback(payload)
    except PaymentProviderError:
        _record(db, provider, "callback", "malformed", {"note": "unparseable payload"})
        return "malformed"

    payment_id = db.scalar(
        select(Payment.id).where(
            Payment.provider == provider.name,
            Payment.provider_reference == callback.checkout_request_id,
        )
    )
    if payment_id is None:
        _record(db, provider, "callback", "unknown_payment", payload)
        return "unknown_payment"

    # Same lock order as publishing and refunds: employer first, then the payment.
    employer_id = db.scalar(select(Payment.employer_profile_id).where(Payment.id == payment_id))
    db.scalar(select(EmployerProfile.id).where(EmployerProfile.id == employer_id).with_for_update())
    payment = db.scalar(
        select(Payment)
        .where(Payment.id == payment_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )

    def finish(outcome: str) -> str:
        _record(db, provider, "callback", outcome, payload, payment_id)
        return outcome

    if payment.status == "succeeded":
        return finish("duplicate")

    now = datetime.now(UTC)

    if callback.result_code != 0:
        payment.callback_received_at = now
        if payment.status == "pending":
            payment.status = "cancelled" if callback.result_code == CANCELLED_BY_USER else "failed"
        db.flush()
        return finish("payment_failed")

    if callback.amount is None or callback.amount != payment.amount or not callback.receipt:
        return finish("rejected_invalid_success")

    receipt_owner = db.scalar(
        select(Payment.id).where(
            Payment.mpesa_receipt_number == callback.receipt,
            Payment.id != payment_id,
        )
    )
    if receipt_owner is not None:
        return finish("duplicate_receipt")

    try:
        status = provider.query_stk_status(callback.checkout_request_id)
    except PaymentProviderError:
        return finish("verification_unavailable")

    _record(
        db,
        provider,
        "status_query",
        "queried",
        {
            "result_code": status.result_code,
            "amount": None if status.amount is None else str(status.amount),
            "receipt": status.receipt,
        },
        payment_id,
    )
    if (
        status.result_code != 0
        or status.amount != payment.amount
        or status.receipt != callback.receipt
    ):
        return finish("verification_failed")

    if not payment.credits_to_grant:
        return finish("needs_review")

    entry_type = (
        "initial_grant" if payment.purpose == "initial_employer_package" else "additional_purchase"
    )
    try:
        with db.begin_nested():
            payment.status = "succeeded"
            payment.verified_at = now
            payment.callback_received_at = now
            payment.mpesa_receipt_number = callback.receipt
            db.add(
                EmployerPostingCreditEntry(
                    employer_profile_id=payment.employer_profile_id,
                    entry_type=entry_type,
                    credit_delta=payment.credits_to_grant,
                    payment_id=payment.id,
                )
            )
            db.flush()
    except IntegrityError:
        return finish("needs_review")

    return finish("granted")
