from app.core.config import settings
from app.core.errors import AppError
from app.services.payment_providers import FakePaymentProvider, PaymentProvider


def get_payment_provider() -> PaymentProvider:
    """Return the configured payment provider, refusing the fake one in production."""
    if settings.payment_provider == "fake" and settings.app_environment != "production":
        return FakePaymentProvider()

    raise AppError(
        status_code=503,
        code="PAYMENT_PROVIDER_UNAVAILABLE",
        message="Payments are not available right now.",
    )
