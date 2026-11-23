from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
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


@dataclass(frozen=True)
class StkStatus:
    result_code: int
    amount: Decimal | None = None
    receipt: str | None = None


@dataclass(frozen=True)
class StkCallback:
    checkout_request_id: str
    result_code: int
    amount: Decimal | None = None
    receipt: str | None = None


def parse_stk_callback(payload: object) -> StkCallback:
    """Parse an STK callback strictly, raising PaymentProviderError if malformed."""
    try:
        body = payload["Body"]["stkCallback"]  # type: ignore[index]
        checkout = body["CheckoutRequestID"]
        result_code = int(body["ResultCode"])
    except (KeyError, TypeError, ValueError) as exc:
        raise PaymentProviderError("Malformed callback payload.") from exc

    if not isinstance(checkout, str) or not 0 < len(checkout) <= 160:
        raise PaymentProviderError("Malformed callback payload.")

    amount: Decimal | None = None
    receipt: str | None = None
    metadata = body.get("CallbackMetadata")
    items = metadata.get("Item") if isinstance(metadata, dict) else None
    if isinstance(items, list):
        for item in items:
            if not isinstance(item, dict):
                continue
            name, value = item.get("Name"), item.get("Value")
            if name == "Amount" and value is not None:
                try:
                    amount = Decimal(str(value))
                except InvalidOperation as exc:
                    raise PaymentProviderError("Malformed callback payload.") from exc
            elif name == "MpesaReceiptNumber" and isinstance(value, str):
                if 0 < len(value) <= 40:
                    receipt = value

    return StkCallback(
        checkout_request_id=checkout,
        result_code=result_code,
        amount=amount,
        receipt=receipt,
    )


class PaymentProvider(Protocol):
    name: str
    environment: str

    def initiate_stk_push(self, request: StkPushRequest) -> StkPushResult: ...

    def query_stk_status(self, checkout_request_id: str) -> StkStatus: ...


class FakePaymentProvider:
    """Offline provider used for development and tests. Never contacts Safaricom."""

    name = "fake"
    environment = "sandbox"

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.requests: list[StkPushRequest] = []
        self.statuses: dict[str, StkStatus] = {}
        self.queried: list[str] = []

    def initiate_stk_push(self, request: StkPushRequest) -> StkPushResult:
        self.requests.append(request)
        if self.fail:
            raise PaymentProviderError("Simulated provider failure.")
        return StkPushResult(
            checkout_request_id=f"fake-checkout-{uuid4().hex}",
            merchant_request_id=f"fake-merchant-{uuid4().hex}",
        )

    def query_stk_status(self, checkout_request_id: str) -> StkStatus:
        self.queried.append(checkout_request_id)
        status = self.statuses.get(checkout_request_id)
        if status is None:
            raise PaymentProviderError("No status is available for this checkout request.")
        return status
