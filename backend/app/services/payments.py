from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.phone import normalize_kenyan_mobile
from app.models.employer import EmployerProfile
from app.models.identity import User
from app.models.job_advertisement import JobAdSettings, Payment
from app.services.payment_providers import (
    PaymentProvider,
    PaymentProviderError,
    StkPushRequest,
)

INITIAL_PURPOSE = "initial_employer_package"
ADDITIONAL_PURPOSE = "additional_posting_credit"
PURPOSES = {INITIAL_PURPOSE, ADDITIONAL_PURPOSE}


class PaymentError(Exception):
    """Base class for payment failures."""


class PaymentNotFoundError(PaymentError):
    """Raised when a payment or employer profile does not exist."""


class PaymentValidationError(PaymentError):
    """Raised when payment input or account state is invalid."""


class PaymentConflictError(PaymentError):
    """Raised when a payment conflicts with existing payments."""


def _get_active_employer(db: Session, user_id: UUID) -> EmployerProfile:
    """Retrieve and lock the user's employer profile after checking account state."""
    employer = db.scalar(
        select(EmployerProfile).where(EmployerProfile.user_id == user_id).with_for_update()
    )
    if employer is None:
        raise PaymentNotFoundError("Create an employer profile before making payments.")

    user = db.get(User, user_id)
    if user is None or user.account_status != "active":
        raise PaymentValidationError("An active account is required to make payments.")
    if user.phone_verified_at is None:
        raise PaymentValidationError("Verify your phone number before making payments.")

    return employer


def initiate_payment(
    db: Session,
    user_id: UUID,
    *,
    purpose: str,
    payer_phone: str,
    idempotency_key: str,
    provider: PaymentProvider,
) -> tuple[Payment, bool]:
    """Create a pending payment and ask the provider to prompt the payer.

    Returns the payment and whether it was newly created. Credits are never granted
    here; that happens only after a verified payment confirmation. A provider failure
    marks the payment failed so the employer can retry with a new idempotency key.
    """
    if purpose not in PURPOSES:
        raise PaymentValidationError("Select a valid payment purpose.")

    try:
        phone = normalize_kenyan_mobile(payer_phone)
    except ValueError as exc:
        raise PaymentValidationError(str(exc)) from exc

    key = idempotency_key.strip() if isinstance(idempotency_key, str) else ""
    if not 8 <= len(key) <= 64:
        raise PaymentValidationError("The idempotency key must be 8 to 64 characters.")

    employer = _get_active_employer(db, user_id)
    stored_key = f"{employer.id}:{key}"

    existing = db.scalar(select(Payment).where(Payment.idempotency_key == stored_key))
    if existing is not None:
        if existing.purpose != purpose or existing.payer_phone != phone:
            raise PaymentConflictError(
                "This idempotency key was already used for a different payment request."
            )
        return existing, False

    if purpose == INITIAL_PURPOSE:
        taken = db.scalar(
            select(Payment.id)
            .where(
                Payment.employer_profile_id == employer.id,
                Payment.purpose == INITIAL_PURPOSE,
                Payment.status.in_(("pending", "succeeded")),
            )
            .limit(1)
        )
        if taken is not None:
            raise PaymentConflictError(
                "The initial package has already been purchased or is awaiting payment."
            )
    else:
        pending = db.scalar(
            select(Payment.id)
            .where(
                Payment.employer_profile_id == employer.id,
                Payment.purpose == purpose,
                Payment.status == "pending",
            )
            .limit(1)
        )
        if pending is not None:
            raise PaymentConflictError(
                "A payment for this purchase is already awaiting confirmation."
            )

    settings = db.get(JobAdSettings, 1)
    if settings is None:
        raise PaymentConflictError("Job advertisement settings have not been initialized.")

    if purpose == INITIAL_PURPOSE:
        amount, credits = settings.initial_package_price, settings.initial_package_credits
    else:
        amount, credits = settings.additional_credit_price, 1

    payment = Payment(
        employer_profile_id=employer.id,
        initiated_by_user_id=user_id,
        purpose=purpose,
        provider=provider.name,
        provider_environment=provider.environment,
        idempotency_key=stored_key,
        amount=amount,
        currency_code=settings.currency_code,
        credits_to_grant=credits,
        payer_phone=phone,
        status="pending",
    )
    db.add(payment)
    db.flush()

    try:
        result = provider.initiate_stk_push(
            StkPushRequest(
                amount=amount,
                currency_code=settings.currency_code,
                payer_phone=phone,
                reference=str(payment.id),
                description="MafundiPro posting credits",
            )
        )
    except PaymentProviderError as exc:
        payment.status = "failed"
        payment.provider_metadata = {"initiation_error": str(exc)[:200]}
        db.flush()
        return payment, True

    payment.provider_reference = result.checkout_request_id
    payment.provider_metadata = {"merchant_request_id": result.merchant_request_id}
    db.flush()
    return payment, True


def get_employer_payment(db: Session, user_id: UUID, payment_id: UUID) -> Payment:
    """Retrieve a payment that belongs to the user's employer profile."""
    payment = db.scalar(
        select(Payment)
        .join(EmployerProfile, EmployerProfile.id == Payment.employer_profile_id)
        .where(Payment.id == payment_id, EmployerProfile.user_id == user_id)
    )
    if payment is None:
        raise PaymentNotFoundError("Payment not found.")
    return payment
