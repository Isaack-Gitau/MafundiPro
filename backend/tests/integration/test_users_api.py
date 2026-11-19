from collections.abc import AsyncIterator, Generator
from datetime import UTC, datetime
from uuid import uuid4

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.api.dependencies.database import get_auth_db, get_db
from app.core.config import settings
from app.core.security import hash_password
from app.main import app
from app.models.identity import User, UserRole

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
    """Provide a session for preparing test accounts and roles."""
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


def create_user(
    db: Session,
    password_hash: str,
    *,
    roles: tuple[str, ...] = (),
) -> User:
    """Create an active, phone-verified test account."""
    username = f"users_{uuid4().hex[:12]}"

    user = User(
        full_name="Users API Test Account",
        username=username,
        username_key=username.casefold(),
        phone_number=f"+2547{uuid4().int % 100_000_000:08d}",
        password_hash=password_hash,
        account_status="active",
        phone_verified_at=datetime.now(UTC),
    )

    user.roles.extend(UserRole(role=role) for role in roles)

    db.add(user)
    db.flush()

    return user


async def login_user(
    client: AsyncClient,
    username: str,
) -> None:
    """Authenticate a test account and retain its session cookie."""
    response = await client.post(
        "/api/v1/auth/login",
        json={
            "username": username,
            "password": "Correct-Horse-Battery-2026!",
        },
    )

    assert response.status_code == 200


async def test_get_me_requires_authentication(
    client: AsyncClient,
) -> None:
    response = await client.get("/api/v1/users/me")

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "AUTHENTICATION_REQUIRED"
    assert response.headers.get("X-Request-ID")


async def test_get_me_returns_safe_authenticated_account(
    client: AsyncClient,
    db_session: Session,
    valid_password_hash: str,
) -> None:
    user = create_user(db_session, valid_password_hash)

    await login_user(client, user.username)

    response = await client.get("/api/v1/users/me")

    assert response.status_code == 200

    data = response.json()

    assert data == {
        "id": str(user.id),
        "full_name": user.full_name,
        "username": user.username,
        "phone_number": user.phone_number,
        "account_status": "active",
        "phone_verified_at": data["phone_verified_at"],
        "created_at": data["created_at"],
        "updated_at": data["updated_at"],
        "platform_roles": [],
    }

    assert data["phone_verified_at"]
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


async def test_get_me_returns_assigned_platform_roles(
    client: AsyncClient,
    db_session: Session,
    valid_password_hash: str,
) -> None:
    user = create_user(
        db_session,
        valid_password_hash,
        roles=("moderator", "admin"),
    )

    await login_user(client, user.username)

    response = await client.get("/api/v1/users/me")

    assert response.status_code == 200
    assert response.json()["platform_roles"] == ["admin", "moderator"]


async def test_get_me_rejects_untrusted_origin(
    client: AsyncClient,
    db_session: Session,
    valid_password_hash: str,
) -> None:
    user = create_user(db_session, valid_password_hash)

    await login_user(client, user.username)

    response = await client.get(
        "/api/v1/users/me",
        headers={"Origin": "https://untrusted.example"},
    )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "UNTRUSTED_ORIGIN"
