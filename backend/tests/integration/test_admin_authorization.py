from collections.abc import AsyncIterator, Generator
from datetime import UTC, datetime
from uuid import uuid4

import pytest
import pytest_asyncio
from fastapi import APIRouter, FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.api.dependencies.authorization import AdminUser
from app.api.dependencies.database import get_auth_db, get_db
from app.core.config import settings
from app.core.errors import AppError, app_error_handler
from app.core.security import hash_password
from app.models.identity import User, UserRole
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
    """Provide a session for preparing test accounts and roles."""
    with test_database() as session:
        yield session


@pytest_asyncio.fixture
async def client(test_database) -> AsyncIterator[AsyncClient]:
    """Provide an HTTP client for the admin-protected test endpoint."""
    test_app = FastAPI()
    test_app.add_exception_handler(AppError, app_error_handler)

    router = APIRouter()

    @router.get("/admin-only")
    def admin_only(current_user: AdminUser) -> dict[str, str]:
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


@pytest.fixture(scope="session")
def valid_password_hash() -> str:
    """Reuse one password hash across authorization tests."""
    return hash_password("Correct-Horse-Battery-2026!")


def create_authenticated_user(
    db: Session,
    password_hash: str,
    *,
    roles: tuple[str, ...] = (),
) -> tuple[User, str]:
    """Create a verified account, optional roles, and a valid session."""
    username = f"admin_auth_{uuid4().hex[:12]}"

    user = User(
        full_name="Admin Authorization Test User",
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

    session_token, _ = create_session(db, user.id)
    db.commit()

    return user, session_token


async def test_admin_endpoint_requires_authentication(
    client: AsyncClient,
) -> None:
    response = await client.get("/admin-only")

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "AUTHENTICATION_REQUIRED"


async def test_admin_role_can_access_admin_endpoint(
    client: AsyncClient,
    db_session: Session,
    valid_password_hash: str,
) -> None:
    user, session_token = create_authenticated_user(
        db_session,
        valid_password_hash,
        roles=("admin",),
    )
    client.cookies.set(settings.session_cookie_name, session_token)

    response = await client.get("/admin-only")

    assert response.status_code == 200
    assert response.json() == {
        "id": str(user.id),
        "username": user.username,
    }


async def test_moderator_cannot_access_admin_endpoint(
    client: AsyncClient,
    db_session: Session,
    valid_password_hash: str,
) -> None:
    _, session_token = create_authenticated_user(
        db_session,
        valid_password_hash,
        roles=("moderator",),
    )
    client.cookies.set(settings.session_cookie_name, session_token)

    response = await client.get("/admin-only")

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "ADMIN_REQUIRED"


async def test_ordinary_user_cannot_access_admin_endpoint(
    client: AsyncClient,
    db_session: Session,
    valid_password_hash: str,
) -> None:
    _, session_token = create_authenticated_user(
        db_session,
        valid_password_hash,
    )
    client.cookies.set(settings.session_cookie_name, session_token)

    response = await client.get("/admin-only")

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "ADMIN_REQUIRED"
