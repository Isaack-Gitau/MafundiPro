from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol
from uuid import uuid4


class PaymentProviderError(Exception):
    """Raised when a payment provider cannot start or confirm a payment."""


@dataclass(frozen=True)
class StkPushRequest:
    amount: Decimal
    currency_code: str
    payer_phone: str
    reference: str
    description: str


@dataclass(frozen=True)
class StkPushResult:
    checkout_request_id: str
    merchant_request_id: str | None = None


class PaymentProvider(Protocol):
    name: str
    environment: str

    def initiate_stk_push(self, request: StkPushRequest) -> StkPushResult: ...


class FakePaymentProvider:
    """Offline provider used for development and tests. Never contacts Safaricom."""

    name = "fake"
    environment = "sandbox"

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.requests: list[StkPushRequest] = []

    def initiate_stk_push(self, request: StkPushRequest) -> StkPushResult:
        self.requests.append(request)
        if self.fail:
            raise PaymentProviderError("Simulated provider failure.")
        return StkPushResult(
            checkout_request_id=f"fake-checkout-{uuid4().hex}",
            merchant_request_id=f"fake-merchant-{uuid4().hex}",
        )
