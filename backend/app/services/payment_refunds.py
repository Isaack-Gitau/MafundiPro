from decimal import ROUND_HALF_UP, Decimal
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.employer import EmployerProfile
from app.models.job_advertisement import (
    EmployerPostingCreditEntry,
    Payment,
    PaymentRefund,
)


class PaymentRefundError(Exception):
    """Base class for payment refund failures."""


class PaymentRefundNotFoundError(PaymentRefundError):
    """Raised when a payment does not exist."""


class PaymentRefundValidationError(PaymentRefundError):
    """Raised when refund input is invalid."""


class PaymentRefundConflictError(PaymentRefundError):
    """Raised when a payment cannot be refunded in its current state."""


def refund_payment(
    db: Session,
    actor_user_id: UUID,
    payment_id: UUID,
    reason: str,
) -> PaymentRefund:
    """Refund a payment's unconsumed credits and reverse them in the ledger.

    The refund amount is the unconsumed credits multiplied by the unit price paid,
    derived from the payment amount and the credits it granted. Consumed credits are
    never refunded and each payment can be refunded only once. Authorization is the
    caller's responsibility.
    """
    if not isinstance(reason, str) or not reason.strip():
        raise PaymentRefundValidationError("A refund reason is required.")
    reason = reason.strip()

    payment = db.get(Payment, payment_id)
    if payment is None:
        raise PaymentRefundNotFoundError("Payment not found.")

    # Same lock order as publishing: employer first, then the payment.
    db.scalar(
        select(EmployerProfile.id)
        .where(EmployerProfile.id == payment.employer_profile_id)
        .with_for_update()
    )
    payment = db.scalar(
        select(Payment)
        .where(Payment.id == payment_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )

    if payment.status != "succeeded":
        raise PaymentRefundConflictError("Only succeeded payments can be refunded.")

    if db.scalar(select(PaymentRefund.id).where(PaymentRefund.payment_id == payment.id)):
        raise PaymentRefundConflictError("This payment has already been refunded.")

    grant = db.scalar(
        select(EmployerPostingCreditEntry).where(
            EmployerPostingCreditEntry.payment_id == payment.id
        )
    )
    if grant is None:
        raise PaymentRefundConflictError("This payment granted no posting credits.")

    used = db.scalar(
        select(func.coalesce(func.sum(EmployerPostingCreditEntry.credit_delta), 0)).where(
            EmployerPostingCreditEntry.source_payment_id == payment.id
        )
    )
    remaining = grant.credit_delta + int(used or 0)
    if remaining < 1:
        raise PaymentRefundConflictError("All credits from this payment have been used.")

    amount = (payment.amount * remaining / grant.credit_delta).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    )

    refund = PaymentRefund(
        payment_id=payment.id,
        employer_profile_id=payment.employer_profile_id,
        credits_refunded=remaining,
        amount=amount,
        currency_code=payment.currency_code,
        reason=reason,
        refunded_by_user_id=actor_user_id,
    )
    db.add(refund)
    db.flush()

    db.add(
        EmployerPostingCreditEntry(
            employer_profile_id=payment.employer_profile_id,
            entry_type="refund_reversal",
            credit_delta=-remaining,
            source_payment_id=payment.id,
            refund_id=refund.id,
        )
    )
    db.flush()
    return refund
