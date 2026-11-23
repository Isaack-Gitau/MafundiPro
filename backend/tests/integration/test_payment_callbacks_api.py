from collections.abc import Generator
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from fastapi import status
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.api.dependencies.database import get_auth_db, get_db
from app.api.dependencies.payments import get_payment_provider
from app.core.config import settings
from app.core.csrf import generate_csrf_token
from app.main import app
from app.models.identity import User
from app.models.job_advertisement import Payment
from app.services.employers import create_employer_profile
from app.services.payment_providers import FakePaymentProvider, StkStatus
from app.services.sessions import create_session

SECRET = "test-callback-secret-value"
PAYMENTS_URL = "/api/v1/employers/payments"
CALLBACK_URL = "/api/v1/payments/mpesa/callback"
BALANCE_URL = "/api/v1/employers/advertisements/credits/balance"


@pytest.fixture
def test_database() -> Generator[sessionmaker]:
    engine = create_engine(
        settings.database_url.set(database="mafundipro_test"),
        pool_pre_ping=True,
    )
    connection = engine.connect()
    transaction = connection.begin()
    factory = sessionmaker(
        bind=connection,
        autoflush=False,
        expire_on_commit=False,
        join_transaction_mode="create_savepoint",
    )
    try:
        yield factory
    finally:
        transaction.rollback()
        connection.close()
        engine.dispose()


@pytest.fixture
def db_session(test_database: sessionmaker) -> Generator[Session]:
    db = test_database()
    try:
        yield db
    finally:
        db.close()


@pytest_asyncio.fixture
async def client(test_database: sessionmaker) -> Generator[AsyncClient]:
    def override_database() -> Generator[Session]:
        db = test_database()
        try:
            yield db
        finally:
            db.close()

    original_overrides = app.dependency_overrides.copy()
    app.dependency_overrides[get_db] = override_database
    app.dependency_overrides[get_auth_db] = override_database

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
    ) as test_client:
        try:
            yield test_client
        finally:
            app.dependency_overrides.clear()
            app.dependency_overrides.update(original_overrides)


@pytest.fixture(autouse=True)
def callback_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "payment_callback_secret", SecretStr(SECRET))


def create_test_employer(db: Session) -> User:
    unique_id = uuid4().hex[:12]
    user = User(
        full_name="Callback API Test User",
        username=f"cbapi_{unique_id}",
        username_key=f"cbapi_{unique_id}",
        phone_number=f"+2547{uuid4().int % 100_000_000:08d}",
        password_hash="test-password-hash",
        account_status="active",
        phone_verified_at=datetime.now(UTC),
    )
    db.add(user)
    db.flush()
    create_employer_profile(db, user.id, employer_type="individual")
    return user


def authenticate_client(client: AsyncClient, db: Session, user: User) -> dict[str, str]:
    session_token, _ = create_session(db, user.id)
    db.commit()
    csrf_token = generate_csrf_token(session_token)
    client.cookies.set(settings.session_cookie_name, session_token)
    client.cookies.set(settings.csrf_cookie_name, csrf_token)
    return {"X-CSRF-Token": csrf_token}


def callback_body(checkout: str, *, code: int = 0, amount: float = 50.0) -> dict:
    body = {
        "MerchantRequestID": "merchant-1",
        "CheckoutRequestID": checkout,
        "ResultCode": code,
        "ResultDesc": "result",
    }
    if code == 0:
        body["CallbackMetadata"] = {
            "Item": [
                {"Name": "Amount", "Value": amount},
                {"Name": "MpesaReceiptNumber", "Value": "RCPT999XYZ"},
                {"Name": "PhoneNumber", "Value": 254712345678},
            ]
        }
    return {"Body": {"stkCallback": body}}


async def start_payment(client, db, provider) -> tuple[User, str, str]:
    app.dependency_overrides[get_payment_provider] = lambda: provider
    user = create_test_employer(db)
    headers = authenticate_client(client, db, user)
    response = await client.post(
        PAYMENTS_URL,
        json={
            "purpose": "initial_employer_package",
            "payer_phone": "0712345678",
            "idempotency_key": f"cb-key-{uuid4().hex}",
        },
        headers=headers,
    )
    assert response.status_code == status.HTTP_201_CREATED, response.text
    payment_id = response.json()["id"]
    payment = db.scalar(select(Payment).where(Payment.id == UUID(payment_id)))
    return user, payment_id, payment.provider_reference


async def credits(client, db, user) -> int:
    authenticate_client(client, db, user)
    response = await client.get(BALANCE_URL)
    assert response.status_code == status.HTTP_200_OK, response.text
    return response.json()["available_credits"]


@pytest.mark.asyncio
async def test_verified_callback_grants_credits_without_a_session(
    client: AsyncClient, db_session: Session
) -> None:
    provider = FakePaymentProvider()
    user, _, checkout = await start_payment(client, db_session, provider)
    provider.statuses[checkout] = StkStatus(0, Decimal("50"), "RCPT999XYZ")
    client.cookies.clear()

    first = await client.post(f"{CALLBACK_URL}/{SECRET}", json=callback_body(checkout))
    replay = await client.post(f"{CALLBACK_URL}/{SECRET}", json=callback_body(checkout))

    assert first.status_code == status.HTTP_200_OK, first.text
    assert first.json() == {"ResultCode": 0, "ResultDesc": "Accepted"}
    assert replay.status_code == status.HTTP_200_OK
    assert await credits(client, db_session, user) == 5


@pytest.mark.asyncio
async def test_wrong_secret_is_not_found_and_grants_nothing(
    client: AsyncClient, db_session: Session
) -> None:
    provider = FakePaymentProvider()
    user, _, checkout = await start_payment(client, db_session, provider)
    provider.statuses[checkout] = StkStatus(0, Decimal("50"), "RCPT999XYZ")

    response = await client.post(
        f"{CALLBACK_URL}/wrong-secret-value-123", json=callback_body(checkout)
    )

    assert response.status_code == status.HTTP_404_NOT_FOUND
    assert await credits(client, db_session, user) == 0


@pytest.mark.asyncio
async def test_unconfigured_secret_disables_the_endpoint(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    app.dependency_overrides[get_payment_provider] = lambda: FakePaymentProvider()
    monkeypatch.setattr(settings, "payment_callback_secret", None)

    response = await client.post(f"{CALLBACK_URL}/{SECRET}", json=callback_body("x"))

    assert response.status_code == status.HTTP_404_NOT_FOUND


@pytest.mark.asyncio
async def test_malformed_callback_is_a_bad_request(client: AsyncClient) -> None:
    app.dependency_overrides[get_payment_provider] = lambda: FakePaymentProvider()

    response = await client.post(f"{CALLBACK_URL}/{SECRET}", json={"Body": {}})

    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert response.json()["error"]["code"] == "MALFORMED_CALLBACK"


@pytest.mark.asyncio
async def test_failed_callback_marks_payment_failed_without_credits(
    client: AsyncClient, db_session: Session
) -> None:
    provider = FakePaymentProvider()
    user, payment_id, checkout = await start_payment(client, db_session, provider)

    response = await client.post(f"{CALLBACK_URL}/{SECRET}", json=callback_body(checkout, code=1))

    assert response.status_code == status.HTTP_200_OK
    authenticate_client(client, db_session, user)
    read = await client.get(f"{PAYMENTS_URL}/{payment_id}")
    assert read.json()["status"] == "failed"
    assert await credits(client, db_session, user) == 0
