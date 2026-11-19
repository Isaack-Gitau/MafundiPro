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
from app.services.taxonomy import create_category, create_skill

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
    return hash_password("Correct-Horse-Battery-2026!")


def create_admin_user(
    db: Session,
    password_hash: str,
) -> User:
    """Create a verified administrator account."""
    username = f"taxonomy_admin_{uuid4().hex[:12]}"

    user = User(
        full_name="Taxonomy Administrator",
        username=username,
        username_key=username.casefold(),
        phone_number=f"+2547{uuid4().int % 100_000_000:08d}",
        password_hash=password_hash,
        account_status="active",
        phone_verified_at=datetime.now(UTC),
    )
    user.roles.append(UserRole(role="admin"))

    db.add(user)
    db.commit()

    return user


async def login_admin(
    client: AsyncClient,
    db: Session,
    password_hash: str,
) -> str:
    """Create and authenticate an administrator, returning the CSRF token."""
    user = create_admin_user(db, password_hash)

    response = await client.post(
        "/api/v1/auth/login",
        json={
            "username": user.username,
            "password": "Correct-Horse-Battery-2026!",
        },
    )

    assert response.status_code == 200
    return response.json()["csrf_token"]


def csrf_headers(token: str) -> dict[str, str]:
    """Build headers for an authenticated state-changing request."""
    return {"X-CSRF-Token": token}


async def test_admin_category_list_requires_admin(
    client: AsyncClient,
) -> None:
    response = await client.get("/api/v1/admin/taxonomy/categories")

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "AUTHENTICATION_REQUIRED"


async def test_admin_category_list_includes_inactive_categories(
    client: AsyncClient,
    db_session: Session,
    valid_password_hash: str,
) -> None:
    csrf_token = await login_admin(client, db_session, valid_password_hash)

    marker = uuid4().hex[:12]
    active = create_category(db_session, f"Active {marker}")
    inactive = create_category(db_session, f"Inactive {marker}")
    inactive.is_active = False
    db_session.commit()

    response = await client.get("/api/v1/admin/taxonomy/categories")

    assert response.status_code == 200

    returned_ids = {item["id"] for item in response.json()}
    assert str(active.id) in returned_ids
    assert str(inactive.id) in returned_ids
    assert csrf_token


async def test_admin_can_create_nested_categories(
    client: AsyncClient,
    db_session: Session,
    valid_password_hash: str,
) -> None:
    csrf_token = await login_admin(client, db_session, valid_password_hash)
    marker = uuid4().hex[:12]

    parent_response = await client.post(
        "/api/v1/admin/taxonomy/categories",
        headers=csrf_headers(csrf_token),
        json={
            "name": f"Construction {marker}",
            "description": " Main trade category ",
            "display_order": 2,
        },
    )

    assert parent_response.status_code == 201
    parent = parent_response.json()
    assert parent["name"] == f"Construction {marker}"
    assert parent["description"] == "Main trade category"
    assert parent["parent_id"] is None

    child_response = await client.post(
        "/api/v1/admin/taxonomy/categories",
        headers=csrf_headers(csrf_token),
        json={
            "name": f"Roofing {marker}",
            "parent_id": parent["id"],
            "display_order": 1,
        },
    )

    assert child_response.status_code == 201
    child = child_response.json()
    assert child["parent_id"] == parent["id"]


async def test_admin_can_update_category_and_clear_description(
    client: AsyncClient,
    db_session: Session,
    valid_password_hash: str,
) -> None:
    csrf_token = await login_admin(client, db_session, valid_password_hash)
    category = create_category(
        db_session,
        f"Original {uuid4().hex[:12]}",
        description="Old description",
    )
    db_session.commit()

    response = await client.patch(
        f"/api/v1/admin/taxonomy/categories/{category.id}",
        headers=csrf_headers(csrf_token),
        json={
            "name": f"Updated {uuid4().hex[:12]}",
            "description": None,
            "display_order": 5,
        },
    )

    assert response.status_code == 200
    assert response.json()["name"].startswith("Updated ")
    assert response.json()["description"] is None
    assert response.json()["display_order"] == 5


async def test_admin_cannot_update_category_with_explicit_null_name(
    client: AsyncClient,
    db_session: Session,
    valid_password_hash: str,
) -> None:
    csrf_token = await login_admin(client, db_session, valid_password_hash)
    category = create_category(db_session, f"Category {uuid4().hex[:12]}")
    db_session.commit()

    response = await client.patch(
        f"/api/v1/admin/taxonomy/categories/{category.id}",
        headers=csrf_headers(csrf_token),
        json={"name": None},
    )

    assert response.status_code == 422


async def test_admin_cannot_create_duplicate_sibling_categories(
    client: AsyncClient,
    db_session: Session,
    valid_password_hash: str,
) -> None:
    csrf_token = await login_admin(client, db_session, valid_password_hash)
    name = f"Duplicate {uuid4().hex[:12]}"

    first = await client.post(
        "/api/v1/admin/taxonomy/categories",
        headers=csrf_headers(csrf_token),
        json={"name": name},
    )
    assert first.status_code == 201

    second = await client.post(
        "/api/v1/admin/taxonomy/categories",
        headers=csrf_headers(csrf_token),
        json={"name": name.swapcase()},
    )

    assert second.status_code == 409
    assert second.json()["error"]["code"] == "TAXONOMY_CONFLICT"


async def test_admin_cannot_move_category_under_its_descendant(
    client: AsyncClient,
    db_session: Session,
    valid_password_hash: str,
) -> None:
    csrf_token = await login_admin(client, db_session, valid_password_hash)
    marker = uuid4().hex[:12]

    parent = create_category(db_session, f"Parent {marker}")
    child = create_category(
        db_session,
        f"Child {marker}",
        parent_id=parent.id,
    )
    db_session.commit()

    response = await client.put(
        f"/api/v1/admin/taxonomy/categories/{parent.id}/parent",
        headers=csrf_headers(csrf_token),
        json={"parent_id": str(child.id)},
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "INVALID_TAXONOMY_INPUT"


async def test_admin_can_deactivate_and_reactivate_category(
    client: AsyncClient,
    db_session: Session,
    valid_password_hash: str,
) -> None:
    csrf_token = await login_admin(client, db_session, valid_password_hash)
    category = create_category(db_session, f"Category {uuid4().hex[:12]}")
    db_session.commit()

    deactivate = await client.patch(
        f"/api/v1/admin/taxonomy/categories/{category.id}/activation",
        headers=csrf_headers(csrf_token),
        json={"is_active": False},
    )

    assert deactivate.status_code == 200
    assert deactivate.json()["is_active"] is False

    reactivate = await client.patch(
        f"/api/v1/admin/taxonomy/categories/{category.id}/activation",
        headers=csrf_headers(csrf_token),
        json={"is_active": True},
    )

    assert reactivate.status_code == 200
    assert reactivate.json()["is_active"] is True


async def test_admin_cannot_delete_category_with_children(
    client: AsyncClient,
    db_session: Session,
    valid_password_hash: str,
) -> None:
    csrf_token = await login_admin(client, db_session, valid_password_hash)
    marker = uuid4().hex[:12]

    parent = create_category(db_session, f"Parent {marker}")
    create_category(db_session, f"Child {marker}", parent_id=parent.id)
    db_session.commit()

    response = await client.delete(
        f"/api/v1/admin/taxonomy/categories/{parent.id}",
        headers=csrf_headers(csrf_token),
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "TAXONOMY_CONFLICT"


async def test_admin_can_create_update_and_deactivate_skill(
    client: AsyncClient,
    db_session: Session,
    valid_password_hash: str,
) -> None:
    csrf_token = await login_admin(client, db_session, valid_password_hash)
    marker = uuid4().hex[:12]

    created = await client.post(
        "/api/v1/admin/taxonomy/skills",
        headers=csrf_headers(csrf_token),
        json={
            "name": f"Installation {marker}",
            "description": "Initial description",
        },
    )

    assert created.status_code == 201
    skill_id = created.json()["id"]

    updated = await client.patch(
        f"/api/v1/admin/taxonomy/skills/{skill_id}",
        headers=csrf_headers(csrf_token),
        json={
            "name": f"Electrical Installation {marker}",
            "description": None,
        },
    )

    assert updated.status_code == 200
    assert updated.json()["name"] == f"Electrical Installation {marker}"
    assert updated.json()["description"] is None

    deactivated = await client.patch(
        f"/api/v1/admin/taxonomy/skills/{skill_id}/activation",
        headers=csrf_headers(csrf_token),
        json={"is_active": False},
    )

    assert deactivated.status_code == 200
    assert deactivated.json()["is_active"] is False


async def test_admin_can_associate_and_disassociate_skill(
    client: AsyncClient,
    db_session: Session,
    valid_password_hash: str,
) -> None:
    csrf_token = await login_admin(client, db_session, valid_password_hash)
    marker = uuid4().hex[:12]

    category = create_category(db_session, f"Category {marker}")
    skill = create_skill(db_session, f"Skill {marker}")
    db_session.commit()

    associated = await client.post(
        f"/api/v1/admin/taxonomy/categories/{category.id}/skills",
        headers=csrf_headers(csrf_token),
        json={"skill_id": str(skill.id)},
    )

    assert associated.status_code == 200
    assert {item["id"] for item in associated.json()["skills"]} == {str(skill.id)}

    disassociated = await client.delete(
        f"/api/v1/admin/taxonomy/categories/{category.id}/skills/{skill.id}",
        headers=csrf_headers(csrf_token),
    )

    assert disassociated.status_code == 200
    assert disassociated.json()["skills"] == []


async def test_admin_can_delete_unused_category_and_skill(
    client: AsyncClient,
    db_session: Session,
    valid_password_hash: str,
) -> None:
    csrf_token = await login_admin(client, db_session, valid_password_hash)
    marker = uuid4().hex[:12]

    category = create_category(db_session, f"Unused Category {marker}")
    skill = create_skill(db_session, f"Unused Skill {marker}")
    db_session.commit()

    category_response = await client.delete(
        f"/api/v1/admin/taxonomy/categories/{category.id}",
        headers=csrf_headers(csrf_token),
    )

    skill_response = await client.delete(
        f"/api/v1/admin/taxonomy/skills/{skill.id}",
        headers=csrf_headers(csrf_token),
    )

    assert category_response.status_code == 204
    assert skill_response.status_code == 204


async def test_admin_write_requires_valid_csrf_token(
    client: AsyncClient,
    db_session: Session,
    valid_password_hash: str,
) -> None:
    await login_admin(client, db_session, valid_password_hash)

    response = await client.post(
        "/api/v1/admin/taxonomy/categories",
        json={"name": f"Rejected {uuid4().hex[:12]}"},
    )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "INVALID_CSRF_TOKEN"


async def test_admin_skill_list_includes_inactive_skills(
    client: AsyncClient,
    db_session: Session,
    valid_password_hash: str,
) -> None:
    csrf_token = await login_admin(client, db_session, valid_password_hash)
    active = create_skill(db_session, f"Active Skill {uuid4().hex[:12]}")
    inactive = create_skill(db_session, f"Inactive Skill {uuid4().hex[:12]}")
    inactive.is_active = False
    db_session.commit()

    response = await client.get("/api/v1/admin/taxonomy/skills")

    assert response.status_code == 200
    returned_ids = {item["id"] for item in response.json()}
    assert str(active.id) in returned_ids
    assert str(inactive.id) in returned_ids
    assert csrf_token


async def test_admin_category_tree_includes_nested_children(
    client: AsyncClient,
    db_session: Session,
    valid_password_hash: str,
) -> None:
    await login_admin(client, db_session, valid_password_hash)
    marker = uuid4().hex[:12]

    root = create_category(db_session, f"Root {marker}")
    child = create_category(db_session, f"Child {marker}", parent_id=root.id)
    grandchild = create_category(db_session, f"Grandchild {marker}", parent_id=child.id)
    db_session.commit()

    response = await client.get("/api/v1/admin/taxonomy/categories")

    assert response.status_code == 200
    roots = response.json()
    root_result = next(item for item in roots if item["id"] == str(root.id))
    child_result = root_result["children"][0]

    assert child_result["id"] == str(child.id)
    assert child_result["children"][0]["id"] == str(grandchild.id)


async def test_admin_category_and_skill_lists_require_admin_role(
    client: AsyncClient,
    db_session: Session,
    valid_password_hash: str,
) -> None:
    username = f"taxonomy_user_{uuid4().hex[:12]}"
    user = User(
        full_name="Ordinary Test User",
        username=username,
        username_key=username.casefold(),
        phone_number=f"+2547{uuid4().int % 100_000_000:08d}",
        password_hash=valid_password_hash,
        account_status="active",
        phone_verified_at=datetime.now(UTC),
    )
    db_session.add(user)
    db_session.commit()

    login_response = await client.post(
        "/api/v1/auth/login",
        json={
            "username": username,
            "password": "Correct-Horse-Battery-2026!",
        },
    )
    assert login_response.status_code == 200

    categories_response = await client.get("/api/v1/admin/taxonomy/categories")
    skills_response = await client.get("/api/v1/admin/taxonomy/skills")

    assert categories_response.status_code == 403
    assert skills_response.status_code == 403
    assert categories_response.json()["error"]["code"] == "ADMIN_REQUIRED"
    assert skills_response.json()["error"]["code"] == "ADMIN_REQUIRED"
