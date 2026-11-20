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
from app.services.sessions import create_session

pytestmark = pytest.mark.asyncio

BASE_URL = "/api/v1/admin/locations"
TEST_PASSWORD = "Correct-Horse-Battery-2026!"


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
    """Provide a session for preparing test data."""
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
    return hash_password(TEST_PASSWORD)


def create_user(
    db: Session,
    password_hash: str,
    *,
    is_admin: bool,
) -> tuple[User, str]:
    """Create a verified account and authenticated session."""
    username = f"location_admin_{uuid4().hex[:12]}"

    user = User(
        full_name="Location Test User",
        username=username,
        username_key=username.casefold(),
        phone_number=f"+2547{uuid4().int % 100_000_000:08d}",
        password_hash=password_hash,
        account_status="active",
        phone_verified_at=datetime.now(UTC),
    )

    if is_admin:
        user.roles.append(UserRole(role="admin"))

    db.add(user)
    db.flush()

    session_token, _ = create_session(db, user.id)
    db.commit()

    return user, session_token


async def login_admin(
    client: AsyncClient,
    db: Session,
    password_hash: str,
) -> str:
    """Create and authenticate an administrator, returning the CSRF token."""
    user, _ = create_user(db, password_hash, is_admin=True)

    response = await client.post(
        "/api/v1/auth/login",
        json={
            "username": user.username,
            "password": TEST_PASSWORD,
        },
    )

    assert response.status_code == 200
    return response.json()["csrf_token"]


def csrf_headers(token: str) -> dict[str, str]:
    """Build headers for an authenticated state-changing request."""
    return {"X-CSRF-Token": token}


async def create_country(
    client: AsyncClient,
    csrf_token: str,
    *,
    name: str | None = None,
) -> dict:
    """Create a country through the administrative API."""
    country_number = uuid4().int % (26 * 26)
    country_code = chr(ord("A") + country_number // 26) + chr(ord("A") + country_number % 26)

    response = await client.post(
        BASE_URL,
        headers=csrf_headers(csrf_token),
        json={
            "location_type": "country",
            "name": name or f"Test Country {uuid4().hex[:10]}",
            "country_code": country_code,
        },
    )

    assert response.status_code == 201, response.text
    return response.json()


async def create_child_location(
    client: AsyncClient,
    csrf_token: str,
    parent_id: str,
    *,
    location_type: str = "county",
    name: str | None = None,
) -> dict:
    """Create a nested location through the administrative API."""
    response = await client.post(
        BASE_URL,
        headers=csrf_headers(csrf_token),
        json={
            "location_type": location_type,
            "name": name or f"Test {location_type} {uuid4().hex[:10]}",
            "parent_id": parent_id,
        },
    )

    assert response.status_code == 201, response.text
    return response.json()


async def test_admin_location_list_requires_authentication(
    client: AsyncClient,
) -> None:
    response = await client.get(BASE_URL)

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "AUTHENTICATION_REQUIRED"


async def test_ordinary_user_cannot_access_admin_locations(
    client: AsyncClient,
    db_session: Session,
    valid_password_hash: str,
) -> None:
    _, session_token = create_user(
        db_session,
        valid_password_hash,
        is_admin=False,
    )
    client.cookies.set(settings.session_cookie_name, session_token)

    response = await client.get(BASE_URL)

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "ADMIN_REQUIRED"


async def test_admin_location_tree_includes_inactive_locations_and_coordinates(
    client: AsyncClient,
    db_session: Session,
    valid_password_hash: str,
) -> None:
    csrf_token = await login_admin(
        client,
        db_session,
        valid_password_hash,
    )

    country = await create_country(client, csrf_token)
    county = await create_child_location(
        client,
        csrf_token,
        country["id"],
    )
    estate = await create_child_location(
        client,
        csrf_token,
        county["id"],
        location_type="estate",
    )

    update_response = await client.patch(
        f"{BASE_URL}/{estate['id']}",
        headers=csrf_headers(csrf_token),
        json={
            "latitude": -1.2634,
            "longitude": 36.8062,
        },
    )
    assert update_response.status_code == 200, update_response.text

    deactivate_response = await client.patch(
        f"{BASE_URL}/{county['id']}/activation",
        headers=csrf_headers(csrf_token),
        json={"is_active": False},
    )
    assert deactivate_response.status_code == 200

    response = await client.get(BASE_URL)

    assert response.status_code == 200
    roots = response.json()

    returned_country = next(item for item in roots if item["id"] == country["id"])
    returned_county = next(
        item for item in returned_country["children"] if item["id"] == county["id"]
    )
    returned_estate = next(
        item for item in returned_county["children"] if item["id"] == estate["id"]
    )

    assert returned_county["is_active"] is False
    assert returned_estate["latitude"] == "-1.263400"
    assert returned_estate["longitude"] == "36.8062000"


async def test_admin_can_create_and_update_locations(
    client: AsyncClient,
    db_session: Session,
    valid_password_hash: str,
) -> None:
    csrf_token = await login_admin(
        client,
        db_session,
        valid_password_hash,
    )

    country = await create_country(client, csrf_token)
    county_name = f"Original County {uuid4().hex[:10]}"

    county = await create_child_location(
        client,
        csrf_token,
        country["id"],
        name=county_name,
    )

    assert county["parent_id"] == country["id"]
    assert county["location_type"] == "county"

    updated_name = f"Updated County {uuid4().hex[:10]}"

    response = await client.patch(
        f"{BASE_URL}/{county['id']}",
        headers=csrf_headers(csrf_token),
        json={
            "name": updated_name,
            "description": "County description",
            "display_order": 4,
        },
    )

    assert response.status_code == 200, response.text
    assert response.json()["name"] == updated_name
    assert response.json()["description"] == "County description"
    assert response.json()["display_order"] == 4

    clear_response = await client.patch(
        f"{BASE_URL}/{county['id']}",
        headers=csrf_headers(csrf_token),
        json={"description": None},
    )

    assert clear_response.status_code == 200
    assert clear_response.json()["description"] is None


async def test_admin_can_move_and_reactivate_locations(
    client: AsyncClient,
    db_session: Session,
    valid_password_hash: str,
) -> None:
    csrf_token = await login_admin(
        client,
        db_session,
        valid_password_hash,
    )

    country = await create_country(client, csrf_token)
    first_county = await create_child_location(
        client,
        csrf_token,
        country["id"],
        name=f"First County {uuid4().hex[:10]}",
    )
    second_county = await create_child_location(
        client,
        csrf_token,
        country["id"],
        name=f"Second County {uuid4().hex[:10]}",
    )
    sub_county = await create_child_location(
        client,
        csrf_token,
        first_county["id"],
        location_type="sub_county",
    )

    move_response = await client.put(
        f"{BASE_URL}/{sub_county['id']}/parent",
        headers=csrf_headers(csrf_token),
        json={"parent_id": second_county["id"]},
    )

    assert move_response.status_code == 200, move_response.text
    assert move_response.json()["parent_id"] == second_county["id"]

    deactivate_response = await client.patch(
        f"{BASE_URL}/{sub_county['id']}/activation",
        headers=csrf_headers(csrf_token),
        json={"is_active": False},
    )

    assert deactivate_response.status_code == 200
    assert deactivate_response.json()["is_active"] is False

    reactivate_response = await client.patch(
        f"{BASE_URL}/{sub_county['id']}/activation",
        headers=csrf_headers(csrf_token),
        json={"is_active": True},
    )

    assert reactivate_response.status_code == 200
    assert reactivate_response.json()["is_active"] is True


async def test_admin_cannot_reactivate_location_under_inactive_parent(
    client: AsyncClient,
    db_session: Session,
    valid_password_hash: str,
) -> None:
    csrf_token = await login_admin(
        client,
        db_session,
        valid_password_hash,
    )

    country = await create_country(client, csrf_token)
    county = await create_child_location(
        client,
        csrf_token,
        country["id"],
    )
    town = await create_child_location(
        client,
        csrf_token,
        county["id"],
        location_type="town",
    )

    await client.patch(
        f"{BASE_URL}/{county['id']}/activation",
        headers=csrf_headers(csrf_token),
        json={"is_active": False},
    )

    await client.patch(
        f"{BASE_URL}/{town['id']}/activation",
        headers=csrf_headers(csrf_token),
        json={"is_active": False},
    )

    response = await client.patch(
        f"{BASE_URL}/{town['id']}/activation",
        headers=csrf_headers(csrf_token),
        json={"is_active": True},
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "INVALID_LOCATION_INPUT"


async def test_admin_can_delete_leaf_but_not_parent_with_children(
    client: AsyncClient,
    db_session: Session,
    valid_password_hash: str,
) -> None:
    csrf_token = await login_admin(
        client,
        db_session,
        valid_password_hash,
    )

    country = await create_country(client, csrf_token)
    county = await create_child_location(
        client,
        csrf_token,
        country["id"],
    )
    town = await create_child_location(
        client,
        csrf_token,
        county["id"],
        location_type="town",
    )

    parent_delete_response = await client.delete(
        f"{BASE_URL}/{county['id']}",
        headers=csrf_headers(csrf_token),
    )

    assert parent_delete_response.status_code == 409
    assert parent_delete_response.json()["error"]["code"] == "LOCATION_CONFLICT"

    leaf_delete_response = await client.delete(
        f"{BASE_URL}/{town['id']}",
        headers=csrf_headers(csrf_token),
    )

    assert leaf_delete_response.status_code == 204


async def test_admin_location_write_requires_valid_csrf_token(
    client: AsyncClient,
    db_session: Session,
    valid_password_hash: str,
) -> None:
    await login_admin(client, db_session, valid_password_hash)

    response = await client.post(
        BASE_URL,
        json={
            "location_type": "country",
            "name": f"Rejected Country {uuid4().hex[:10]}",
            "country_code": "ZZ",
        },
    )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "INVALID_CSRF_TOKEN"
