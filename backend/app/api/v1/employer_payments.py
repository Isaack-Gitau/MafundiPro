from typing import Annotated, cast
from uuid import UUID

from fastapi import APIRouter, Depends, Response, status

from app.api.dependencies.authentication import CurrentUser
from app.api.dependencies.database import DatabaseSession
from app.api.dependencies.payments import get_payment_provider
from app.core.errors import AppError
from app.models.job_advertisement import Payment
from app.schemas.payments import PaymentCreate, PaymentPurpose, PaymentRead, PaymentStatus
from app.services.payment_providers import PaymentProvider
from app.services.payments import (
    PaymentConflictError,
    PaymentError,
    PaymentNotFoundError,
    PaymentValidationError,
    get_employer_payment,
    initiate_payment,
)

router = APIRouter()


def _translate_payment_error(exc: PaymentError) -> AppError:
    if isinstance(exc, PaymentNotFoundError):
        return AppError(status_code=404, code="PAYMENT_NOT_FOUND", message=str(exc))
    if isinstance(exc, PaymentConflictError):
        return AppError(status_code=409, code="PAYMENT_CONFLICT", message=str(exc))
    if isinstance(exc, PaymentValidationError):
        return AppError(status_code=422, code="INVALID_PAYMENT", message=str(exc))
    return AppError(
        status_code=500,
        code="PAYMENT_ERROR",
        message="The payment could not be processed.",
    )


def _mask_phone(phone: str | None) -> str | None:
    if not phone:
        return None
    return phone[:4] + "*" * (len(phone) - 7) + phone[-3:]


def _serialize(payment: Payment) -> PaymentRead:
    return PaymentRead(
        id=payment.id,
        purpose=cast(PaymentPurpose, payment.purpose),
        status=cast(PaymentStatus, payment.status),
        amount=payment.amount,
        currency_code=payment.currency_code,
        credits_to_grant=payment.credits_to_grant,
        payer_phone_masked=_mask_phone(payment.payer_phone),
        created_at=payment.created_at,
    )


@router.post(
    "",
    response_model=PaymentRead,
    status_code=status.HTTP_201_CREATED,
    summary="Start a posting-credit payment",
)
def create_payment(
    payload: PaymentCreate,
    response: Response,
    current_user: CurrentUser,
    db: DatabaseSession,
    provider: Annotated[PaymentProvider, Depends(get_payment_provider)],
) -> PaymentRead:
    """Create a pending payment and prompt the payer. Credits arrive after verification."""
    try:
        payment, created = initiate_payment(
            db,
            current_user.id,
            purpose=payload.purpose,
            payer_phone=payload.payer_phone,
            idempotency_key=payload.idempotency_key,
            provider=provider,
        )
        db.commit()
    except PaymentError as exc:
        db.rollback()
        raise _translate_payment_error(exc) from exc

    if not created:
        response.status_code = status.HTTP_200_OK
    elif payment.status == "failed":
        raise AppError(
            status_code=502,
            code="PAYMENT_PROVIDER_ERROR",
            message="The payment could not be started. Please try again.",
        )

    return _serialize(payment)


@router.get(
    "/{payment_id}",
    response_model=PaymentRead,
    summary="View one of your payments",
)
def read_payment(
    payment_id: UUID,
    current_user: CurrentUser,
    db: DatabaseSession,
) -> PaymentRead:
    try:
        payment = get_employer_payment(db, current_user.id, payment_id)
    except PaymentError as exc:
        raise _translate_payment_error(exc) from exc

    return _serialize(payment)
