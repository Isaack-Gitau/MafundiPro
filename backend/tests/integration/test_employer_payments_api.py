from collections.abc import Generator
from datetime import UTC, datetime
from uuid import uuid4

import pytest
import pytest_asyncio
from fastapi import status
from httpx import ASGITransport, AsyncClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.api.dependencies.database import get_auth_db, get_db
from app.api.dependencies.payments import get_payment_provider
from app.core.config import settings
from app.core.csrf import generate_csrf_token
from app.main import app
from app.models.identity import User
from app.services.employers import create_employer_profile
from app.services.payment_providers import FakePaymentProvider
from app.services.sessions import create_session

URL = "/api/v1/employers/payments"


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


def create_test_employer(db: Session) -> User:
    unique_id = uuid4().hex[:12]
    user = User(
        full_name="Payment API Test User",
        username=f"payapi_{unique_id}",
        username_key=f"payapi_{unique_id}",
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


def payload(**overrides) -> dict:
    body = {
        "purpose": "initial_employer_package",
        "payer_phone": "0712345678",
        "idempotency_key": f"api-key-{uuid4().hex}",
    }
    body.update(overrides)
    return body


def use_provider(provider: FakePaymentProvider) -> None:
    app.dependency_overrides[get_payment_provider] = lambda: provider


@pytest.mark.asyncio
async def test_payment_endpoints_require_authentication(client: AsyncClient) -> None:
    response = await client.post(URL, json=payload())

    assert response.status_code == status.HTTP_401_UNAUTHORIZED


@pytest.mark.asyncio
async def test_payment_creation_requires_csrf_token(
    client: AsyncClient, db_session: Session
) -> None:
    user = create_test_employer(db_session)
    authenticate_client(client, db_session, user)

    response = await client.post(URL, json=payload())

    assert response.status_code == status.HTTP_403_FORBIDDEN


@pytest.mark.asyncio
async def test_employer_can_start_payment_with_masked_phone(
    client: AsyncClient, db_session: Session
) -> None:
    use_provider(FakePaymentProvider())
    user = create_test_employer(db_session)
    headers = authenticate_client(client, db_session, user)

    response = await client.post(URL, json=payload(), headers=headers)

    assert response.status_code == status.HTTP_201_CREATED, response.text
    body = response.json()
    assert body["status"] == "pending"
    assert body["credits_to_grant"] == 5
    assert body["payer_phone_masked"] == "+254******678"
    assert "712345678" not in response.text


@pytest.mark.asyncio
async def test_replayed_request_returns_same_payment(
    client: AsyncClient, db_session: Session
) -> None:
    provider = FakePaymentProvider()
    use_provider(provider)
    user = create_test_employer(db_session)
    headers = authenticate_client(client, db_session, user)
    body = payload()

    first = await client.post(URL, json=body, headers=headers)
    second = await client.post(URL, json=body, headers=headers)

    assert first.status_code == status.HTTP_201_CREATED
    assert second.status_code == status.HTTP_200_OK
    assert second.json()["id"] == first.json()["id"]
    assert len(provider.requests) == 1


@pytest.mark.asyncio
async def test_employer_can_read_own_payment_but_not_anothers(
    client: AsyncClient, db_session: Session
) -> None:
    use_provider(FakePaymentProvider())
    owner = create_test_employer(db_session)
    owner_headers = authenticate_client(client, db_session, owner)
    created = await client.post(URL, json=payload(), headers=owner_headers)
    payment_id = created.json()["id"]

    own = await client.get(f"{URL}/{payment_id}")
    assert own.status_code == status.HTTP_200_OK

    other = create_test_employer(db_session)
    authenticate_client(client, db_session, other)
    foreign = await client.get(f"{URL}/{payment_id}")
    assert foreign.status_code == status.HTTP_404_NOT_FOUND


@pytest.mark.asyncio
async def test_second_pending_payment_is_a_conflict(
    client: AsyncClient, db_session: Session
) -> None:
    use_provider(FakePaymentProvider())
    user = create_test_employer(db_session)
    headers = authenticate_client(client, db_session, user)
    await client.post(URL, json=payload(), headers=headers)

    response = await client.post(URL, json=payload(), headers=headers)

    assert response.status_code == status.HTTP_409_CONFLICT
    assert response.json()["error"]["code"] == "PAYMENT_CONFLICT"


@pytest.mark.asyncio
async def test_invalid_payer_phone_is_rejected(client: AsyncClient, db_session: Session) -> None:
    use_provider(FakePaymentProvider())
    user = create_test_employer(db_session)
    headers = authenticate_client(client, db_session, user)

    response = await client.post(URL, json=payload(payer_phone="12345"), headers=headers)

    assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT


@pytest.mark.asyncio
async def test_provider_failure_returns_bad_gateway_and_allows_retry(
    client: AsyncClient, db_session: Session
) -> None:
    use_provider(FakePaymentProvider(fail=True))
    user = create_test_employer(db_session)
    headers = authenticate_client(client, db_session, user)

    failed = await client.post(URL, json=payload(), headers=headers)
    assert failed.status_code == status.HTTP_502_BAD_GATEWAY
    assert failed.json()["error"]["code"] == "PAYMENT_PROVIDER_ERROR"

    use_provider(FakePaymentProvider())
    retry = await client.post(URL, json=payload(), headers=headers)
    assert retry.status_code == status.HTTP_201_CREATED
