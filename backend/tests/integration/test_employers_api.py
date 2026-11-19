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
from app.models.employer import EmployerProfile
from app.models.identity import User

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
    username = f"employer_{uuid4().hex[:12]}"

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


async def test_create_employer_profile_requires_authentication(
    client: AsyncClient,
) -> None:
    response = await client.post(
        "/api/v1/employers/me",
        json={"employer_type": "individual"},
    )

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "AUTHENTICATION_REQUIRED"


async def test_get_employer_profile_requires_authentication(
    client: AsyncClient,
) -> None:
    response = await client.get("/api/v1/employers/me")

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "AUTHENTICATION_REQUIRED"


async def test_update_employer_profile_requires_authentication(
    client: AsyncClient,
) -> None:
    response = await client.patch(
        "/api/v1/employers/me",
        json={"display_name": "@Updated"},
    )

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "AUTHENTICATION_REQUIRED"


async def test_create_employer_profile_requires_csrf_token(
    client: AsyncClient,
    db_session: Session,
    valid_password_hash: str,
) -> None:
    user = create_user(db_session, valid_password_hash)
    await login_user(client, user.username)

    response = await client.post(
        "/api/v1/employers/me",
        json={"employer_type": "individual"},
    )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "INVALID_CSRF_TOKEN"


async def test_update_employer_profile_requires_csrf_token(
    client: AsyncClient,
    db_session: Session,
    valid_password_hash: str,
) -> None:
    user = create_user(db_session, valid_password_hash)
    await login_user(client, user.username)

    response = await client.patch(
        "/api/v1/employers/me",
        json={"display_name": "@Updated"},
    )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "INVALID_CSRF_TOKEN"


async def test_create_employer_profile_initializes_expected_values(
    client: AsyncClient,
    db_session: Session,
    valid_password_hash: str,
) -> None:
    user = create_user(db_session, valid_password_hash)
    csrf_token = await login_user(client, user.username)

    response = await client.post(
        "/api/v1/employers/me",
        headers={"X-CSRF-Token": csrf_token},
        json={
            "employer_type": "business",
            "description": "  A local construction business  ",
        },
    )

    assert response.status_code == 201

    data = response.json()

    assert data["user_id"] == str(user.id)
    assert data["employer_type"] == "business"
    assert data["display_name"] == "@Worker"
    assert data["description"] == "A local construction business"
    assert data["id"]
    assert data["created_at"]
    assert data["updated_at"]


async def test_create_employer_profile_allows_missing_description(
    client: AsyncClient,
    db_session: Session,
    valid_password_hash: str,
) -> None:
    user = create_user(db_session, valid_password_hash)
    csrf_token = await login_user(client, user.username)

    response = await client.post(
        "/api/v1/employers/me",
        headers={"X-CSRF-Token": csrf_token},
        json={"employer_type": "individual"},
    )

    assert response.status_code == 201
    assert response.json()["description"] is None


async def test_create_employer_profile_rejects_duplicate(
    client: AsyncClient,
    db_session: Session,
    valid_password_hash: str,
) -> None:
    user = create_user(db_session, valid_password_hash)
    csrf_token = await login_user(client, user.username)

    first = await client.post(
        "/api/v1/employers/me",
        headers={"X-CSRF-Token": csrf_token},
        json={"employer_type": "individual"},
    )

    assert first.status_code == 201

    duplicate = await client.post(
        "/api/v1/employers/me",
        headers={"X-CSRF-Token": csrf_token},
        json={"employer_type": "business"},
    )

    assert duplicate.status_code == 409
    assert duplicate.json()["error"]["code"] == "EMPLOYER_PROFILE_EXISTS"


async def test_get_employer_profile_returns_not_found_when_missing(
    client: AsyncClient,
    db_session: Session,
    valid_password_hash: str,
) -> None:
    user = create_user(db_session, valid_password_hash)
    await login_user(client, user.username)

    response = await client.get("/api/v1/employers/me")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "EMPLOYER_PROFILE_NOT_FOUND"


async def test_get_employer_profile_returns_created_profile(
    client: AsyncClient,
    db_session: Session,
    valid_password_hash: str,
) -> None:
    user = create_user(db_session, valid_password_hash)
    csrf_token = await login_user(client, user.username)

    created = await client.post(
        "/api/v1/employers/me",
        headers={"X-CSRF-Token": csrf_token},
        json={
            "employer_type": "organization",
            "description": "Community organization",
        },
    )

    assert created.status_code == 201

    response = await client.get("/api/v1/employers/me")

    assert response.status_code == 200
    assert response.json() == created.json()


async def test_employer_profile_response_excludes_sensitive_user_fields(
    client: AsyncClient,
    db_session: Session,
    valid_password_hash: str,
) -> None:
    user = create_user(db_session, valid_password_hash)
    csrf_token = await login_user(client, user.username)

    response = await client.post(
        "/api/v1/employers/me",
        headers={"X-CSRF-Token": csrf_token},
        json={"employer_type": "contractor"},
    )

    assert response.status_code == 201

    sensitive_fields = {
        "password_hash",
        "phone_number",
        "full_name",
        "username",
        "username_key",
        "account_status",
        "phone_verified_at",
        "token_version",
        "role",
        "roles",
        "token_hash",
        "csrf_token",
    }

    assert sensitive_fields.isdisjoint(response.json().keys())


async def test_employer_profile_endpoints_reject_untrusted_origin(
    client: AsyncClient,
    db_session: Session,
    valid_password_hash: str,
) -> None:
    user = create_user(db_session, valid_password_hash)
    csrf_token = await login_user(client, user.username)

    get_response = await client.get(
        "/api/v1/employers/me",
        headers={"Origin": "https://untrusted.example"},
    )

    post_response = await client.post(
        "/api/v1/employers/me",
        headers={
            "Origin": "https://untrusted.example",
            "X-CSRF-Token": csrf_token,
        },
        json={"employer_type": "individual"},
    )

    patch_response = await client.patch(
        "/api/v1/employers/me",
        headers={
            "Origin": "https://untrusted.example",
            "X-CSRF-Token": csrf_token,
        },
        json={"display_name": "@Updated"},
    )

    for response in (get_response, post_response, patch_response):
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "UNTRUSTED_ORIGIN"


async def test_update_employer_profile_returns_not_found_when_missing(
    client: AsyncClient,
    db_session: Session,
    valid_password_hash: str,
) -> None:
    user = create_user(db_session, valid_password_hash)
    csrf_token = await login_user(client, user.username)

    response = await client.patch(
        "/api/v1/employers/me",
        headers={"X-CSRF-Token": csrf_token},
        json={"display_name": "@Updated"},
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "EMPLOYER_PROFILE_NOT_FOUND"


async def test_update_employer_profile_changes_supplied_fields_only(
    client: AsyncClient,
    db_session: Session,
    valid_password_hash: str,
) -> None:
    user = create_user(db_session, valid_password_hash)
    csrf_token = await login_user(client, user.username)

    created = await client.post(
        "/api/v1/employers/me",
        headers={"X-CSRF-Token": csrf_token},
        json={
            "employer_type": "individual",
            "description": "Initial description",
        },
    )

    assert created.status_code == 201

    response = await client.patch(
        "/api/v1/employers/me",
        headers={"X-CSRF-Token": csrf_token},
        json={
            "employer_type": "contractor",
            "display_name": "  @Updated Employer  ",
            "description": "  Updated description  ",
        },
    )

    assert response.status_code == 200

    data = response.json()

    assert data["employer_type"] == "contractor"
    assert data["display_name"] == "@Updated Employer"
    assert data["description"] == "Updated description"
    assert data["user_id"] == str(user.id)
    assert data["id"] == created.json()["id"]


async def test_update_employer_profile_can_clear_description(
    client: AsyncClient,
    db_session: Session,
    valid_password_hash: str,
) -> None:
    user = create_user(db_session, valid_password_hash)
    csrf_token = await login_user(client, user.username)

    created = await client.post(
        "/api/v1/employers/me",
        headers={"X-CSRF-Token": csrf_token},
        json={
            "employer_type": "business",
            "description": "Business description",
        },
    )

    assert created.status_code == 201

    response = await client.patch(
        "/api/v1/employers/me",
        headers={"X-CSRF-Token": csrf_token},
        json={"description": "   "},
    )

    assert response.status_code == 200
    assert response.json()["description"] is None


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"employer_type": None},
        {"employer_type": "invalid"},
        {"display_name": None},
        {"display_name": ""},
        {"display_name": "   "},
        {"display_name": "A" * 121},
        {"description": 123},
        {"unknown_field": "not allowed"},
    ],
)
async def test_update_employer_profile_rejects_invalid_payloads(
    client: AsyncClient,
    db_session: Session,
    valid_password_hash: str,
    payload: dict[str, object],
) -> None:
    user = create_user(db_session, valid_password_hash)
    csrf_token = await login_user(client, user.username)

    created = await client.post(
        "/api/v1/employers/me",
        headers={"X-CSRF-Token": csrf_token},
        json={"employer_type": "individual"},
    )

    assert created.status_code == 201

    response = await client.patch(
        "/api/v1/employers/me",
        headers={"X-CSRF-Token": csrf_token},
        json=payload,
    )

    assert response.status_code == 422


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"employer_type": None},
        {"employer_type": "invalid"},
        {"unknown_field": "not allowed"},
    ],
)
async def test_create_employer_profile_rejects_invalid_payloads(
    client: AsyncClient,
    db_session: Session,
    valid_password_hash: str,
    payload: dict[str, object],
) -> None:
    user = create_user(db_session, valid_password_hash)
    csrf_token = await login_user(client, user.username)

    response = await client.post(
        "/api/v1/employers/me",
        headers={"X-CSRF-Token": csrf_token},
        json=payload,
    )

    assert response.status_code == 422


async def test_employer_profiles_are_scoped_to_authenticated_user(
    client: AsyncClient,
    db_session: Session,
    valid_password_hash: str,
) -> None:
    first_user = create_user(db_session, valid_password_hash)
    second_user = create_user(db_session, valid_password_hash)

    first_csrf = await login_user(client, first_user.username)

    created = await client.post(
        "/api/v1/employers/me",
        headers={"X-CSRF-Token": first_csrf},
        json={"employer_type": "business"},
    )

    assert created.status_code == 201

    second_csrf = await login_user(client, second_user.username)

    get_response = await client.get("/api/v1/employers/me")

    patch_response = await client.patch(
        "/api/v1/employers/me",
        headers={"X-CSRF-Token": second_csrf},
        json={"display_name": "@Unauthorized"},
    )

    assert get_response.status_code == 404
    assert get_response.json()["error"]["code"] == "EMPLOYER_PROFILE_NOT_FOUND"

    assert patch_response.status_code == 404
    assert patch_response.json()["error"]["code"] == "EMPLOYER_PROFILE_NOT_FOUND"

    original_profile = db_session.scalar(
        select(EmployerProfile).where(EmployerProfile.user_id == first_user.id)
    )

    assert original_profile is not None
    assert original_profile.display_name == "@Worker"
    assert original_profile.user_id == first_user.id
