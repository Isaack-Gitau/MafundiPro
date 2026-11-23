from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import exists, func, select
from sqlalchemy.orm import Session

from app.models.employer import EmployerProfile
from app.models.job_advertisement import EmployerPostingCreditEntry, Payment
from app.services.payment_callbacks import (
    CANCELLED_BY_USER,
    grant_verified_payment,
    record_event,
)
from app.services.payment_providers import PaymentProvider, PaymentProviderError

DEFAULT_MIN_AGE = timedelta(minutes=5)
DEFAULT_DELAYED_AFTER = timedelta(hours=24)
GRANT_ENTRY_TYPES = ("initial_grant", "additional_purchase")

ANOMALY_KEYS = (
    "delayed_pending",
    "pending_without_reference",
    "succeeded_without_receipt",
    "succeeded_without_ledger",
    "ledger_without_success",
    "grant_mismatch",
)


def find_due_payment_ids(db: Session, *, min_age: timedelta, limit: int) -> list[UUID]:
    """Return pending payments old enough to reconcile, oldest first."""
    cutoff = datetime.now(UTC) - min_age
    return list(
        db.scalars(
            select(Payment.id)
            .where(
                Payment.status == "pending",
                Payment.provider_reference.is_not(None),
                Payment.created_at <= cutoff,
            )
            .order_by(Payment.created_at)
            .limit(limit)
        )
    )


def reconcile_payment(db: Session, provider: PaymentProvider, payment_id: UUID) -> str:
    """Settle one pending payment from an independent provider status query.

    Applies the same rules as the callback path and records one status_query event.
    Returns an outcome label. The caller commits (or rolls back for a dry run).
    """
    employer_id = db.scalar(select(Payment.employer_profile_id).where(Payment.id == payment_id))
    if employer_id is None:
        return "skipped"

    # Same lock order as publishing, refunds and callbacks: employer first, then payment.
    db.scalar(select(EmployerProfile.id).where(EmployerProfile.id == employer_id).with_for_update())
    payment = db.scalar(
        select(Payment)
        .where(Payment.id == payment_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if payment is None or payment.status != "pending" or not payment.provider_reference:
        return "skipped"

    checkout = payment.provider_reference

    def finish(outcome: str, details: dict) -> str:
        record_event(db, provider, "status_query", outcome, details, payment_id)
        return outcome

    try:
        status = provider.query_stk_status(checkout)
    except PaymentProviderError:
        return finish("verification_unavailable", {"source": "reconciliation"})

    details = {
        "source": "reconciliation",
        "result_code": status.result_code,
        "amount": None if status.amount is None else str(status.amount),
        "receipt": status.receipt,
    }

    if status.result_code != 0:
        payment.status = "cancelled" if status.result_code == CANCELLED_BY_USER else "failed"
        db.flush()
        return finish("payment_failed", details)

    if status.amount is None or status.amount != payment.amount or not status.receipt:
        return finish("rejected_invalid_success", details)

    receipt_owner = db.scalar(
        select(Payment.id).where(
            Payment.mpesa_receipt_number == status.receipt,
            Payment.id != payment_id,
        )
    )
    if receipt_owner is not None:
        return finish("duplicate_receipt", details)

    if not grant_verified_payment(db, payment, status.receipt, datetime.now(UTC)):
        return finish("needs_review", details)

    return finish("granted", details)


def find_anomalies(
    db: Session, *, delayed_after: timedelta = DEFAULT_DELAYED_AFTER
) -> dict[str, list[UUID]]:
    """Report payment and ledger inconsistencies. Read-only: nothing is changed."""
    Entry = EmployerPostingCreditEntry
    grants = Entry.entry_type.in_(GRANT_ENTRY_TYPES)
    delayed_cutoff = datetime.now(UTC) - delayed_after

    totals = (
        select(Entry.payment_id.label("pid"), func.sum(Entry.credit_delta).label("total"))
        .where(Entry.payment_id.is_not(None), grants)
        .group_by(Entry.payment_id)
        .subquery()
    )

    return {
        "delayed_pending": list(
            db.scalars(
                select(Payment.id).where(
                    Payment.status == "pending", Payment.created_at <= delayed_cutoff
                )
            )
        ),
        "pending_without_reference": list(
            db.scalars(
                select(Payment.id).where(
                    Payment.status == "pending", Payment.provider_reference.is_(None)
                )
            )
        ),
        "succeeded_without_receipt": list(
            db.scalars(
                select(Payment.id).where(
                    Payment.status == "succeeded", Payment.mpesa_receipt_number.is_(None)
                )
            )
        ),
        "succeeded_without_ledger": list(
            db.scalars(
                select(Payment.id).where(
                    Payment.status == "succeeded",
                    Payment.credits_to_grant.is_not(None),
                    ~exists().where(grants, Entry.payment_id == Payment.id),
                )
            )
        ),
        "ledger_without_success": list(
            db.scalars(
                select(Entry.payment_id)
                .join(Payment, Payment.id == Entry.payment_id)
                .where(grants, Payment.status != "succeeded")
                .distinct()
            )
        ),
        "grant_mismatch": list(
            db.scalars(
                select(Payment.id)
                .join(totals, totals.c.pid == Payment.id)
                .where(Payment.status == "succeeded", totals.c.total != Payment.credits_to_grant)
            )
        ),
    }
