from collections.abc import AsyncIterator, Generator
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
import pytest_asyncio
from fastapi import APIRouter, FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.api.dependencies.authentication import CurrentUser
from app.api.dependencies.database import get_auth_db, get_db
from app.core.config import settings
from app.core.csrf import generate_csrf_token
from app.core.errors import AppError, app_error_handler
from app.core.security import hash_password, hash_session_token
from app.models.identity import User, UserSession
from app.services.sessions import create_session

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
    """Provide a session for preparing and inspecting test data."""
    with test_database() as session:
        yield session


@pytest_asyncio.fixture
async def client(test_database) -> AsyncIterator[AsyncClient]:
    """Provide an HTTP client for test-only protected endpoints."""
    test_app = FastAPI()
    test_app.add_exception_handler(AppError, app_error_handler)

    router = APIRouter()

    @router.get("/protected")
    def protected_get(current_user: CurrentUser) -> dict[str, str]:
        return {
            "id": str(current_user.id),
            "username": current_user.username,
        }

    @router.post("/protected")
    def protected_post(current_user: CurrentUser) -> dict[str, str]:
        return {
            "id": str(current_user.id),
            "username": current_user.username,
        }

    test_app.include_router(router)

    def override_get_db() -> Generator[Session]:
        with test_database() as session:
            yield session

    def override_get_auth_db() -> Generator[Session]:
        with test_database() as session:
            yield session

    test_app.dependency_overrides[get_db] = override_get_db
    test_app.dependency_overrides[get_auth_db] = override_get_auth_db

    transport = ASGITransport(
        app=test_app,
        client=("127.0.0.1", 12345),
    )

    async with AsyncClient(
        transport=transport,
        base_url="http://testserver",
    ) as test_client:
        yield test_client


@pytest.fixture
def valid_password_hash() -> str:
    """Create a password hash for test accounts."""
    return hash_password("Correct-Horse-Battery-2026!")


def create_authenticated_user(
    db: Session,
    password_hash: str,
    *,
    session_created_at: datetime | None = None,
    revoked: bool = False,
) -> tuple[User, str]:
    """Create a verified account and a corresponding server-managed session."""
    username = f"dependency_{uuid4().hex[:12]}"

    user = User(
        full_name="Dependency Test User",
        username=username,
        username_key=username.casefold(),
        phone_number=f"+2547{uuid4().int % 100_000_000:08d}",
        password_hash=password_hash,
        account_status="active",
        phone_verified_at=datetime.now(UTC),
    )

    db.add(user)
    db.flush()

    session_token, user_session = create_session(
        db,
        user.id,
        now=session_created_at,
    )

    if revoked:
        user_session.revoked_at = datetime.now(UTC)

    db.commit()

    return user, session_token


def authenticate_client(
    client: AsyncClient,
    session_token: str,
) -> None:
    """Set the server-managed session cookie on the test client."""
    client.cookies.set(settings.session_cookie_name, session_token)


async def test_protected_get_rejects_missing_session(
    client: AsyncClient,
) -> None:
    response = await client.get("/protected")

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "AUTHENTICATION_REQUIRED"


async def test_protected_get_accepts_valid_session(
    client: AsyncClient,
    db_session: Session,
    valid_password_hash: str,
) -> None:
    created_at = datetime.now(UTC) - timedelta(minutes=5)

    user, session_token = create_authenticated_user(
        db_session,
        valid_password_hash,
        session_created_at=created_at,
    )
    authenticate_client(client, session_token)

    original_session = db_session.scalar(
        select(UserSession).where(UserSession.token_hash == hash_session_token(session_token))
    )
    assert original_session is not None
    original_expiry = original_session.expires_at

    response = await client.get("/protected")

    assert response.status_code == 200
    assert response.json() == {
        "id": str(user.id),
        "username": user.username,
    }

    db_session.expire_all()

    refreshed_session = db_session.scalar(
        select(UserSession).where(UserSession.token_hash == hash_session_token(session_token))
    )

    assert refreshed_session is not None
    assert refreshed_session.last_seen_at > created_at
    assert refreshed_session.expires_at > original_expiry


async def test_protected_get_rejects_expired_session(
    client: AsyncClient,
    db_session: Session,
    valid_password_hash: str,
) -> None:
    expired_time = datetime.now(UTC) - timedelta(hours=1)

    _, session_token = create_authenticated_user(
        db_session,
        valid_password_hash,
        session_created_at=expired_time,
    )
    authenticate_client(client, session_token)

    response = await client.get("/protected")

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "INVALID_SESSION"

    db_session.expire_all()

    expired_session = db_session.scalar(
        select(UserSession).where(UserSession.token_hash == hash_session_token(session_token))
    )

    assert expired_session is not None
    assert expired_session.revoked_at is not None


async def test_protected_get_rejects_revoked_session(
    client: AsyncClient,
    db_session: Session,
    valid_password_hash: str,
) -> None:
    _, session_token = create_authenticated_user(
        db_session,
        valid_password_hash,
        revoked=True,
    )
    authenticate_client(client, session_token)

    response = await client.get("/protected")

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "INVALID_SESSION"


async def test_protected_post_requires_csrf_token(
    client: AsyncClient,
    db_session: Session,
    valid_password_hash: str,
) -> None:
    _, session_token = create_authenticated_user(
        db_session,
        valid_password_hash,
    )
    authenticate_client(client, session_token)

    response = await client.post("/protected")

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "INVALID_CSRF_TOKEN"


async def test_protected_post_accepts_valid_native_csrf_token(
    client: AsyncClient,
    db_session: Session,
    valid_password_hash: str,
) -> None:
    user, session_token = create_authenticated_user(
        db_session,
        valid_password_hash,
    )
    authenticate_client(client, session_token)

    csrf_token = generate_csrf_token(session_token)

    response = await client.post(
        "/protected",
        headers={"X-CSRF-Token": csrf_token},
    )

    assert response.status_code == 200
    assert response.json()["id"] == str(user.id)


async def test_protected_post_rejects_mismatched_csrf_cookie(
    client: AsyncClient,
    db_session: Session,
    valid_password_hash: str,
) -> None:
    _, session_token = create_authenticated_user(
        db_session,
        valid_password_hash,
    )
    authenticate_client(client, session_token)

    csrf_token = generate_csrf_token(session_token)
    client.cookies.set(settings.csrf_cookie_name, "different-token")

    response = await client.post(
        "/protected",
        headers={"X-CSRF-Token": csrf_token},
    )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "INVALID_CSRF_TOKEN"


async def test_protected_post_rejects_csrf_token_from_another_session(
    client: AsyncClient,
    db_session: Session,
    valid_password_hash: str,
) -> None:
    _, first_session_token = create_authenticated_user(
        db_session,
        valid_password_hash,
    )
    _, second_session_token = create_authenticated_user(
        db_session,
        valid_password_hash,
    )
    authenticate_client(client, first_session_token)

    csrf_token = generate_csrf_token(second_session_token)

    response = await client.post(
        "/protected",
        headers={"X-CSRF-Token": csrf_token},
    )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "INVALID_CSRF_TOKEN"


async def test_protected_get_accepts_configured_origin(
    client: AsyncClient,
    db_session: Session,
    valid_password_hash: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        settings,
        "frontend_origins",
        "https://trusted.example",
    )

    user, session_token = create_authenticated_user(
        db_session,
        valid_password_hash,
    )
    authenticate_client(client, session_token)

    response = await client.get(
        "/protected",
        headers={"Origin": "https://trusted.example"},
    )

    assert response.status_code == 200
    assert response.json()["id"] == str(user.id)


async def test_protected_get_rejects_untrusted_origin(
    client: AsyncClient,
    db_session: Session,
    valid_password_hash: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        settings,
        "frontend_origins",
        "https://trusted.example",
    )

    _, session_token = create_authenticated_user(
        db_session,
        valid_password_hash,
    )
    authenticate_client(client, session_token)

    response = await client.get(
        "/protected",
        headers={"Origin": "https://untrusted.example"},
    )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "UNTRUSTED_ORIGIN"


async def test_protected_post_accepts_valid_browser_csrf_pair(
    client: AsyncClient,
    db_session: Session,
    valid_password_hash: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        settings,
        "frontend_origins",
        "https://trusted.example",
    )

    user, session_token = create_authenticated_user(
        db_session,
        valid_password_hash,
    )
    authenticate_client(client, session_token)

    csrf_token = generate_csrf_token(session_token)
    client.cookies.set(settings.csrf_cookie_name, csrf_token)

    response = await client.post(
        "/protected",
        headers={
            "Origin": "https://trusted.example",
            "X-CSRF-Token": csrf_token,
        },
    )

    assert response.status_code == 200
    assert response.json()["id"] == str(user.id)
