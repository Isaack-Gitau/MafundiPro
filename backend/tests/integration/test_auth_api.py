from collections.abc import AsyncIterator, Generator
from datetime import UTC, datetime
from uuid import uuid4

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.api.dependencies.database import get_db
from app.core.config import settings
from app.core.security import hash_password, hash_session_token
from app.main import app
from app.models.identity import User, UserSession
from app.services.login_rate_limits import record_failed_login

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
    """Provide an HTTP client using the rollback-isolated test database."""
    previous_overrides = app.dependency_overrides.copy()

    def override_get_db() -> Generator[Session]:
        with test_database() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db

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
    """Reuse one Argon2id hash to keep integration tests reasonably fast."""
    return hash_password("Correct-Horse-Battery-2026!")


def create_user(
    db: Session,
    password_hash: str,
    *,
    username: str | None = None,
    account_status: str = "active",
    verified: bool = True,
) -> User:
    """Create a test account with a unique username and phone number."""
    username = username or f"auth_{uuid4().hex[:12]}"

    user = User(
        full_name="Authentication Test User",
        username=username,
        username_key=username.casefold(),
        phone_number=f"+2547{uuid4().int % 100_000_000:08d}",
        password_hash=password_hash,
        account_status=account_status,
        phone_verified_at=datetime.now(UTC) if verified else None,
    )

    db.add(user)
    db.flush()

    return user


def login_payload(username: str) -> dict[str, str]:
    return {
        "username": username,
        "password": "Correct-Horse-Battery-2026!",
    }


async def login_user(
    client: AsyncClient,
    username: str,
):
    return await client.post(
        "/api/v1/auth/login",
        json=login_payload(username),
    )


async def test_login_authenticates_active_verified_user(
    client: AsyncClient,
    db_session: Session,
    valid_password_hash: str,
) -> None:
    user = create_user(db_session, valid_password_hash)

    response = await login_user(client, user.username)

    assert response.status_code == 200

    data = response.json()

    assert data["id"] == str(user.id)
    assert data["full_name"] == user.full_name
    assert data["username"] == user.username
    assert isinstance(data["csrf_token"], str)
    assert data["csrf_token"]
    assert "password" not in data
    assert "password_hash" not in data
    assert settings.session_cookie_name not in data

    session_token = client.cookies.get(settings.session_cookie_name)
    assert session_token

    stored_session = db_session.scalar(select(UserSession).where(UserSession.user_id == user.id))

    assert stored_session is not None
    assert stored_session.token_hash == hash_session_token(session_token)
    assert stored_session.token_hash != session_token
    assert stored_session.revoked_at is None

    set_cookie_headers = response.headers.get_list("set-cookie")
    session_cookie_header = next(
        header
        for header in set_cookie_headers
        if header.startswith(f"{settings.session_cookie_name}=")
    )
    csrf_cookie_header = next(
        header
        for header in set_cookie_headers
        if header.startswith(f"{settings.csrf_cookie_name}=")
    )

    assert "httponly" in session_cookie_header.lower()
    assert "httponly" not in csrf_cookie_header.lower()
    assert "path=/" in session_cookie_header.lower()
    assert "samesite=lax" in session_cookie_header.lower()


async def test_login_is_case_insensitive_for_username(
    client: AsyncClient,
    db_session: Session,
    valid_password_hash: str,
) -> None:
    user = create_user(db_session, valid_password_hash)

    response = await login_user(client, user.username.upper())

    assert response.status_code == 200
    assert response.json()["id"] == str(user.id)


async def test_login_rejects_incorrect_password(
    client: AsyncClient,
    db_session: Session,
    valid_password_hash: str,
) -> None:
    user = create_user(db_session, valid_password_hash)

    response = await client.post(
        "/api/v1/auth/login",
        json={
            "username": user.username,
            "password": "This-is-the-wrong-password!",
        },
    )

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "INVALID_CREDENTIALS"
    assert response.headers.get("X-Request-ID")
    assert settings.session_cookie_name not in response.cookies


async def test_login_rejects_unknown_username(
    client: AsyncClient,
) -> None:
    response = await client.post(
        "/api/v1/auth/login",
        json=login_payload(f"missing_{uuid4().hex[:12]}"),
    )

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "INVALID_CREDENTIALS"
    assert settings.session_cookie_name not in response.cookies


async def test_login_rejects_unverified_account(
    client: AsyncClient,
    db_session: Session,
    valid_password_hash: str,
) -> None:
    user = create_user(
        db_session,
        valid_password_hash,
        verified=False,
    )

    response = await login_user(client, user.username)

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "INVALID_CREDENTIALS"
    assert settings.session_cookie_name not in response.cookies


async def test_login_rejects_suspended_account(
    client: AsyncClient,
    db_session: Session,
    valid_password_hash: str,
) -> None:
    user = create_user(
        db_session,
        valid_password_hash,
        account_status="suspended",
    )

    response = await login_user(client, user.username)

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "INVALID_CREDENTIALS"
    assert settings.session_cookie_name not in response.cookies


async def test_login_rejects_untrusted_origin(
    client: AsyncClient,
) -> None:
    response = await client.post(
        "/api/v1/auth/login",
        json=login_payload(f"origin_{uuid4().hex[:12]}"),
        headers={"Origin": "https://untrusted.example"},
    )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "UNTRUSTED_ORIGIN"


async def test_login_rate_limit_returns_429_and_retry_after(
    client: AsyncClient,
    db_session: Session,
    valid_password_hash: str,
) -> None:
    user = create_user(db_session, valid_password_hash)

    for _ in range(settings.login_rate_limit_username_ip_attempts):
        record_failed_login(
            db_session,
            user.username,
            "127.0.0.1",
        )

    db_session.commit()

    response = await login_user(client, user.username)

    assert response.status_code == 429
    assert response.json()["error"]["code"] == "LOGIN_RATE_LIMITED"
    assert int(response.headers["Retry-After"]) > 0
    assert response.headers.get("X-Request-ID")
    assert settings.session_cookie_name not in response.cookies


async def test_logout_revokes_session_and_clears_cookies(
    client: AsyncClient,
    db_session: Session,
    valid_password_hash: str,
) -> None:
    user = create_user(db_session, valid_password_hash)

    login_response = await login_user(client, user.username)

    assert login_response.status_code == 200

    csrf_token = login_response.json()["csrf_token"]
    session_token = client.cookies.get(settings.session_cookie_name)

    assert session_token

    logout_response = await client.post(
        "/api/v1/auth/logout",
        headers={"X-CSRF-Token": csrf_token},
    )

    assert logout_response.status_code == 200
    assert logout_response.json() == {"logged_out": True}

    stored_session = db_session.scalar(select(UserSession).where(UserSession.user_id == user.id))

    assert stored_session is not None
    assert stored_session.revoked_at is not None

    set_cookie_headers = logout_response.headers.get_list("set-cookie")

    assert any(
        header.startswith(f"{settings.session_cookie_name}=") and "max-age=0" in header.lower()
        for header in set_cookie_headers
    )
    assert any(
        header.startswith(f"{settings.csrf_cookie_name}=") and "max-age=0" in header.lower()
        for header in set_cookie_headers
    )


async def test_logout_rejects_invalid_csrf_token(
    client: AsyncClient,
    db_session: Session,
    valid_password_hash: str,
) -> None:
    user = create_user(db_session, valid_password_hash)

    login_response = await login_user(client, user.username)

    assert login_response.status_code == 200

    response = await client.post(
        "/api/v1/auth/logout",
        headers={"X-CSRF-Token": "invalid-csrf-token"},
    )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "INVALID_CSRF_TOKEN"

    stored_session = db_session.scalar(select(UserSession).where(UserSession.user_id == user.id))

    assert stored_session is not None
    assert stored_session.revoked_at is None


async def test_logout_rejects_mismatched_csrf_cookie_and_header(
    client: AsyncClient,
    db_session: Session,
    valid_password_hash: str,
) -> None:
    user = create_user(db_session, valid_password_hash)

    login_response = await login_user(client, user.username)

    assert login_response.status_code == 200

    csrf_token = login_response.json()["csrf_token"]
    mismatched_token = csrf_token + "tampered"

    response = await client.post(
        "/api/v1/auth/logout",
        headers={"X-CSRF-Token": mismatched_token},
    )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "INVALID_CSRF_TOKEN"


async def test_logout_rejects_untrusted_origin(
    client: AsyncClient,
    db_session: Session,
    valid_password_hash: str,
) -> None:
    user = create_user(db_session, valid_password_hash)

    login_response = await login_user(client, user.username)

    assert login_response.status_code == 200

    csrf_token = login_response.json()["csrf_token"]

    response = await client.post(
        "/api/v1/auth/logout",
        headers={
            "Origin": "https://untrusted.example",
            "X-CSRF-Token": csrf_token,
        },
    )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "UNTRUSTED_ORIGIN"


async def test_logout_without_session_is_idempotent(
    client: AsyncClient,
) -> None:
    response = await client.post("/api/v1/auth/logout")

    assert response.status_code == 200
    assert response.json() == {"logged_out": False}
