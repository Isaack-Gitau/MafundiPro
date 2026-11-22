from datetime import datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.core.phone import normalize_kenyan_mobile

PaymentPurpose = Literal["initial_employer_package", "additional_posting_credit"]
PaymentStatus = Literal["pending", "succeeded", "failed", "cancelled"]


class PaymentCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    purpose: PaymentPurpose
    payer_phone: str = Field(max_length=20)
    idempotency_key: str = Field(min_length=8, max_length=64)

    @field_validator("payer_phone")
    @classmethod
    def validate_payer_phone(cls, value: str) -> str:
        return normalize_kenyan_mobile(value)


class PaymentRead(BaseModel):
    id: UUID
    purpose: PaymentPurpose
    status: PaymentStatus
    amount: Decimal
    currency_code: str
    credits_to_grant: int | None
    payer_phone_masked: str | None
    created_at: datetime
