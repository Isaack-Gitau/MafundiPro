from collections.abc import AsyncIterator, Generator
from uuid import uuid4

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.api.dependencies.database import get_db
from app.core.config import settings
from app.main import app

pytestmark = pytest.mark.asyncio


@pytest.fixture
def test_engine():
    engine = create_engine(
        settings.database_url.set(database="mafundipro_test"),
        pool_pre_ping=True,
    )
    try:
        yield engine
    finally:
        engine.dispose()


@pytest_asyncio.fixture
async def client(test_engine) -> AsyncIterator[AsyncClient]:
    previous_overrides = app.dependency_overrides.copy()

    connection = test_engine.connect()
    outer_transaction = connection.begin()

    test_session_factory = sessionmaker(
        bind=connection,
        autoflush=False,
        expire_on_commit=False,
        join_transaction_mode="create_savepoint",
    )

    def override_get_db() -> Generator[Session]:
        with test_session_factory() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db

    try:
        transport = ASGITransport(app=app)

        async with AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as test_client:
            yield test_client
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(previous_overrides)

        outer_transaction.rollback()
        connection.close()


@pytest.fixture
def registration_details() -> dict[str, str]:
    marker = uuid4().hex[:10]
    phone_suffix = str(uuid4().int % 100_000_000).zfill(8)

    return {
        "full_name": "Registration Test User",
        "username": f"reg_{marker}",
        "phone_number": f"07{phone_suffix}",
        "password": "a-secure-password-123",
    }


async def test_registration_creates_account(
    client: AsyncClient,
    registration_details: dict[str, str],
) -> None:
    response = await client.post(
        "/api/v1/auth/register",
        json=registration_details,
    )

    assert response.status_code == 201

    data = response.json()

    assert data["full_name"] == registration_details["full_name"]
    assert data["username"] == registration_details["username"]
    assert data["phone_number"] == ("+254" + registration_details["phone_number"][1:])
    assert data["account_status"] == "pending_verification"
    assert "id" in data
    assert "created_at" in data

    assert "password" not in data
    assert "password_hash" not in data


async def test_registration_returns_request_id(
    client: AsyncClient,
    registration_details: dict[str, str],
) -> None:
    response = await client.post(
        "/api/v1/auth/register",
        json=registration_details,
    )

    assert response.status_code == 201
    assert response.headers.get("X-Request-ID")


async def test_registration_rejects_duplicate_username(
    client: AsyncClient,
    registration_details: dict[str, str],
) -> None:
    first = await client.post(
        "/api/v1/auth/register",
        json=registration_details,
    )
    assert first.status_code == 201

    duplicate = registration_details.copy()
    duplicate["username"] = duplicate["username"].upper()
    duplicate["phone_number"] = f"01{uuid4().int % 100_000_000:08d}"

    response = await client.post(
        "/api/v1/auth/register",
        json=duplicate,
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "REGISTRATION_CONFLICT"
    assert response.headers.get("X-Request-ID")


async def test_registration_rejects_duplicate_phone_number(
    client: AsyncClient,
    registration_details: dict[str, str],
) -> None:
    first = await client.post(
        "/api/v1/auth/register",
        json=registration_details,
    )
    assert first.status_code == 201

    duplicate = registration_details.copy()
    duplicate["username"] = f"other_{uuid4().hex[:12]}"

    response = await client.post(
        "/api/v1/auth/register",
        json=duplicate,
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "REGISTRATION_CONFLICT"


async def test_registration_rejects_invalid_input(
    client: AsyncClient,
    registration_details: dict[str, str],
) -> None:
    invalid = registration_details.copy()
    invalid["phone_number"] = "12345"

    response = await client.post(
        "/api/v1/auth/register",
        json=invalid,
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"
    assert response.headers.get("X-Request-ID")


async def test_registration_rejects_unexpected_fields(
    client: AsyncClient,
    registration_details: dict[str, str],
) -> None:
    invalid = registration_details.copy()
    invalid["is_admin"] = True

    response = await client.post(
        "/api/v1/auth/register",
        json=invalid,
    )

    assert response.status_code == 422


async def test_registration_does_not_authenticate_user(
    client: AsyncClient,
    registration_details: dict[str, str],
) -> None:
    response = await client.post(
        "/api/v1/auth/register",
        json=registration_details,
    )

    assert response.status_code == 201
    assert "mafundipro_session" not in response.cookies
