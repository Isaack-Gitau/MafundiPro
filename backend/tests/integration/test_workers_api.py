from collections.abc import AsyncIterator, Generator
from datetime import UTC, datetime
from uuid import uuid4

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.api.dependencies.database import get_auth_db, get_db
from app.core.config import settings
from app.core.security import hash_password
from app.main import app
from app.models.identity import User
from app.models.worker import WorkerProfile

pytestmark = pytest.mark.asyncio


@pytest.fixture
def test_database():
    """Provide a rollback-isolated connection to the PostgreSQL test database."""
    engine = create_engine(
        settings.database_url.set(database="mafundipro_test"),
        pool_pre_ping=True,
    )
    connection = engine.connect()
    outer_transaction = connection.begin()

    factory = sessionmaker(
        bind=connection,
        autoflush=False,
        expire_on_commit=False,
        join_transaction_mode="create_savepoint",
    )

    try:
        yield factory
    finally:
        outer_transaction.rollback()
        connection.close()
        engine.dispose()


@pytest.fixture
def db_session(test_database) -> Generator[Session]:
    """Provide a session for preparing test accounts."""
    with test_database() as session:
        yield session


@pytest_asyncio.fixture
async def client(test_database) -> AsyncIterator[AsyncClient]:
    """Provide an HTTP client using the isolated PostgreSQL test database."""
    previous_overrides = app.dependency_overrides.copy()

    def override_get_db() -> Generator[Session]:
        with test_database() as session:
            yield session

    def override_get_auth_db() -> Generator[Session]:
        with test_database() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_auth_db] = override_get_auth_db

    try:
        transport = ASGITransport(
            app=app,
            client=("127.0.0.1", 12345),
        )

        async with AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as test_client:
            yield test_client
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(previous_overrides)


@pytest.fixture(scope="session")
def valid_password_hash() -> str:
    """Reuse one Argon2id hash across integration tests."""
    return hash_password("Correct-Horse-Battery-2026!")


def create_user(db: Session, password_hash: str) -> User:
    """Create an active, phone-verified test account."""
    username = f"worker_{uuid4().hex[:12]}"

    user = User(
        full_name="Worker API Test Account",
        username=username,
        username_key=username.casefold(),
        phone_number=f"+2547{uuid4().int % 100_000_000:08d}",
        password_hash=password_hash,
        account_status="active",
        phone_verified_at=datetime.now(UTC),
    )

    db.add(user)
    db.flush()

    return user


async def login_user(client: AsyncClient, username: str) -> str:
    """Authenticate a test account and return its CSRF token."""
    response = await client.post(
        "/api/v1/auth/login",
        json={
            "username": username,
            "password": "Correct-Horse-Battery-2026!",
        },
    )

    assert response.status_code == 200
    return response.json()["csrf_token"]


async def test_create_worker_profile_requires_authentication(
    client: AsyncClient,
) -> None:
    response = await client.post("/api/v1/workers/me")

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "AUTHENTICATION_REQUIRED"


async def test_get_worker_profile_requires_authentication(
    client: AsyncClient,
) -> None:
    response = await client.get("/api/v1/workers/me")

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "AUTHENTICATION_REQUIRED"


async def test_get_worker_profile_returns_not_found_when_missing(
    client: AsyncClient,
    db_session: Session,
    valid_password_hash: str,
) -> None:
    user = create_user(db_session, valid_password_hash)
    await login_user(client, user.username)

    response = await client.get("/api/v1/workers/me")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "WORKER_PROFILE_NOT_FOUND"

    assert (
        db_session.scalar(select(WorkerProfile.id).where(WorkerProfile.user_id == user.id)) is None
    )


async def test_create_worker_profile_initializes_defaults(
    client: AsyncClient,
    db_session: Session,
    valid_password_hash: str,
) -> None:
    user = create_user(db_session, valid_password_hash)
    csrf_token = await login_user(client, user.username)

    response = await client.post(
        "/api/v1/workers/me",
        headers={"X-CSRF-Token": csrf_token},
    )

    assert response.status_code == 201
    assert response.headers.get("X-Request-ID")

    data = response.json()

    assert data["user_id"] == str(user.id)
    assert data["display_name"] == user.full_name
    assert data["bio"] is None
    assert data["service_description"] is None
    assert data["experience_years"] is None
    assert data["availability"] == "unavailable"
    assert data["id"]
    assert data["created_at"]
    assert data["updated_at"]

    sensitive_fields = {
        "password",
        "password_hash",
        "username_key",
        "sessions",
        "phone_verifications",
        "token",
        "token_hash",
        "csrf_token",
    }

    assert sensitive_fields.isdisjoint(data.keys())


async def test_create_worker_profile_requires_csrf_token(
    client: AsyncClient,
    db_session: Session,
    valid_password_hash: str,
) -> None:
    user = create_user(db_session, valid_password_hash)
    await login_user(client, user.username)

    response = await client.post("/api/v1/workers/me")

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "INVALID_CSRF_TOKEN"


async def test_create_worker_profile_rejects_duplicate(
    client: AsyncClient,
    db_session: Session,
    valid_password_hash: str,
) -> None:
    user = create_user(db_session, valid_password_hash)
    csrf_token = await login_user(client, user.username)

    first = await client.post(
        "/api/v1/workers/me",
        headers={"X-CSRF-Token": csrf_token},
    )

    assert first.status_code == 201

    duplicate = await client.post(
        "/api/v1/workers/me",
        headers={"X-CSRF-Token": csrf_token},
    )

    assert duplicate.status_code == 409
    assert duplicate.json()["error"]["code"] == "WORKER_PROFILE_EXISTS"


async def test_get_worker_profile_returns_created_profile(
    client: AsyncClient,
    db_session: Session,
    valid_password_hash: str,
) -> None:
    user = create_user(db_session, valid_password_hash)
    csrf_token = await login_user(client, user.username)

    created = await client.post(
        "/api/v1/workers/me",
        headers={"X-CSRF-Token": csrf_token},
    )

    assert created.status_code == 201

    response = await client.get("/api/v1/workers/me")

    assert response.status_code == 200
    assert response.json() == created.json()


async def test_worker_profile_endpoints_reject_untrusted_origin(
    client: AsyncClient,
    db_session: Session,
    valid_password_hash: str,
) -> None:
    user = create_user(db_session, valid_password_hash)
    csrf_token = await login_user(client, user.username)

    get_response = await client.get(
        "/api/v1/workers/me",
        headers={"Origin": "https://untrusted.example"},
    )

    post_response = await client.post(
        "/api/v1/workers/me",
        headers={
            "Origin": "https://untrusted.example",
            "X-CSRF-Token": csrf_token,
        },
    )

    assert get_response.status_code == 403
    assert get_response.json()["error"]["code"] == "UNTRUSTED_ORIGIN"

    assert post_response.status_code == 403
    assert post_response.json()["error"]["code"] == "UNTRUSTED_ORIGIN"
