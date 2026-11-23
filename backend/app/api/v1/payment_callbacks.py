import hmac
from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends

from app.api.dependencies.database import DatabaseSession
from app.api.dependencies.payments import get_payment_provider
from app.core.config import settings
from app.core.errors import AppError
from app.services.payment_callbacks import process_mpesa_callback
from app.services.payment_providers import PaymentProvider

router = APIRouter()


def _verify_secret(secret: str) -> None:
    configured = settings.payment_callback_secret
    value = configured.get_secret_value() if configured is not None else ""
    if len(value) < 16 or not hmac.compare_digest(secret.encode(), value.encode()):
        raise AppError(
            status_code=404,
            code="NOT_FOUND",
            message="The requested resource was not found.",
        )


@router.post(
    "/callback/{secret}",
    summary="Receive an M-Pesa payment callback",
    include_in_schema=False,
)
def mpesa_callback(
    secret: str,
    db: DatabaseSession,
    provider: Annotated[PaymentProvider, Depends(get_payment_provider)],
    payload: Annotated[dict[str, Any], Body()],
) -> dict[str, Any]:
    """Accept a provider callback. Credits are granted only after verification."""
    _verify_secret(secret)

    outcome = process_mpesa_callback(db, provider, payload)
    db.commit()

    if outcome == "malformed":
        raise AppError(
            status_code=400,
            code="MALFORMED_CALLBACK",
            message="The callback payload is not valid.",
        )

    return {"ResultCode": 0, "ResultDesc": "Accepted"}
