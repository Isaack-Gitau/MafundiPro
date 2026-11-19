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
from app.models.worker import WorkerProfession, WorkerProfile, WorkerSkill
from app.services.taxonomy import associate_skill, create_category, create_skill

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


def create_user(db: Session, password_hash: str) -> User:
    """Create an active, phone-verified test account."""
    username = f"selection_{uuid4().hex[:12]}"

    user = User(
        full_name="Worker Selection Test Account",
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


async def create_authenticated_worker(
    client: AsyncClient,
    db: Session,
    password_hash: str,
) -> tuple[User, str]:
    """Create, authenticate, and initialize a worker profile."""
    user = create_user(db, password_hash)
    csrf_token = await login_user(client, user.username)

    response = await client.post(
        "/api/v1/workers/me",
        headers={"X-CSRF-Token": csrf_token},
    )

    assert response.status_code == 201

    return user, csrf_token


async def test_get_professions_requires_authentication(
    client: AsyncClient,
) -> None:
    response = await client.get("/api/v1/workers/me/professions")

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "AUTHENTICATION_REQUIRED"


async def test_get_professions_requires_existing_worker_profile(
    client: AsyncClient,
    db_session: Session,
    valid_password_hash: str,
) -> None:
    user = create_user(db_session, valid_password_hash)
    await login_user(client, user.username)

    response = await client.get("/api/v1/workers/me/professions")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "WORKER_PROFILE_NOT_FOUND"


async def test_get_professions_returns_empty_selection(
    client: AsyncClient,
    db_session: Session,
    valid_password_hash: str,
) -> None:
    _, _ = await create_authenticated_worker(
        client,
        db_session,
        valid_password_hash,
    )

    response = await client.get("/api/v1/workers/me/professions")

    assert response.status_code == 200
    assert response.json() == {"professions": []}


async def test_replace_professions_accepts_active_root_categories(
    client: AsyncClient,
    db_session: Session,
    valid_password_hash: str,
) -> None:
    _, csrf_token = await create_authenticated_worker(
        client,
        db_session,
        valid_password_hash,
    )

    construction = create_category(db_session, "Construction")
    electrical = create_category(db_session, "Electrical")
    plumbing = create_category(db_session, "Plumbing")

    response = await client.put(
        "/api/v1/workers/me/professions",
        headers={"X-CSRF-Token": csrf_token},
        json={
            "category_ids": [
                str(construction.id),
                str(electrical.id),
                str(plumbing.id),
            ]
        },
    )

    assert response.status_code == 200

    professions = response.json()["professions"]

    assert len(professions) == 3
    assert {item["category_id"] for item in professions} == {
        str(construction.id),
        str(electrical.id),
        str(plumbing.id),
    }
    assert all(item["skills"] == [] for item in professions)


async def test_replace_professions_rejects_nested_categories(
    client: AsyncClient,
    db_session: Session,
    valid_password_hash: str,
) -> None:
    _, csrf_token = await create_authenticated_worker(
        client,
        db_session,
        valid_password_hash,
    )

    construction = create_category(db_session, "Construction")
    roofing = create_category(
        db_session,
        "Roofing",
        parent_id=construction.id,
    )

    response = await client.put(
        "/api/v1/workers/me/professions",
        headers={"X-CSRF-Token": csrf_token},
        json={"category_ids": [str(roofing.id)]},
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "INVALID_WORKER_SELECTION"


async def test_replace_professions_rejects_more_than_three_categories(
    client: AsyncClient,
    db_session: Session,
    valid_password_hash: str,
) -> None:
    _, csrf_token = await create_authenticated_worker(
        client,
        db_session,
        valid_password_hash,
    )

    categories = [
        create_category(db_session, "Construction"),
        create_category(db_session, "Electrical"),
        create_category(db_session, "Plumbing"),
        create_category(db_session, "Cleaning"),
    ]

    response = await client.put(
        "/api/v1/workers/me/professions",
        headers={"X-CSRF-Token": csrf_token},
        json={"category_ids": [str(category.id) for category in categories]},
    )

    assert response.status_code == 422


async def test_replace_skills_accepts_active_skills_in_profession_subtree(
    client: AsyncClient,
    db_session: Session,
    valid_password_hash: str,
) -> None:
    _, csrf_token = await create_authenticated_worker(
        client,
        db_session,
        valid_password_hash,
    )

    construction = create_category(db_session, "Construction")
    roofing = create_category(
        db_session,
        "Roofing",
        parent_id=construction.id,
    )
    metal_roofing = create_category(
        db_session,
        "Metal Roofing",
        parent_id=roofing.id,
    )

    skill = create_skill(db_session, "Metal Roof Installation")
    associate_skill(db_session, metal_roofing.id, skill.id)

    profession_response = await client.put(
        "/api/v1/workers/me/professions",
        headers={"X-CSRF-Token": csrf_token},
        json={"category_ids": [str(construction.id)]},
    )

    assert profession_response.status_code == 200

    response = await client.put(
        f"/api/v1/workers/me/professions/{construction.id}/skills",
        headers={"X-CSRF-Token": csrf_token},
        json={
            "skills": [
                {
                    "category_id": str(metal_roofing.id),
                    "skill_id": str(skill.id),
                }
            ]
        },
    )

    assert response.status_code == 200

    profession = response.json()["professions"][0]

    assert profession["category_id"] == str(construction.id)
    assert profession["skills"] == [
        {
            "category_id": str(metal_roofing.id),
            "category_name": "Metal Roofing",
            "skill_id": str(skill.id),
            "skill_name": "Metal Roof Installation",
        }
    ]


async def test_replace_skills_rejects_category_outside_profession_subtree(
    client: AsyncClient,
    db_session: Session,
    valid_password_hash: str,
) -> None:
    _, csrf_token = await create_authenticated_worker(
        client,
        db_session,
        valid_password_hash,
    )

    construction = create_category(db_session, "Construction")
    electrical = create_category(db_session, "Electrical")

    skill = create_skill(db_session, "Wiring")
    associate_skill(db_session, electrical.id, skill.id)

    profession_response = await client.put(
        "/api/v1/workers/me/professions",
        headers={"X-CSRF-Token": csrf_token},
        json={"category_ids": [str(construction.id)]},
    )

    assert profession_response.status_code == 200

    response = await client.put(
        f"/api/v1/workers/me/professions/{construction.id}/skills",
        headers={"X-CSRF-Token": csrf_token},
        json={
            "skills": [
                {
                    "category_id": str(electrical.id),
                    "skill_id": str(skill.id),
                }
            ]
        },
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "INVALID_WORKER_SELECTION"


async def test_removing_profession_also_removes_its_selected_skills(
    client: AsyncClient,
    db_session: Session,
    valid_password_hash: str,
) -> None:
    user, csrf_token = await create_authenticated_worker(
        client,
        db_session,
        valid_password_hash,
    )

    construction = create_category(db_session, "Construction")
    roofing = create_category(
        db_session,
        "Roofing",
        parent_id=construction.id,
    )
    electrical = create_category(db_session, "Electrical")

    skill = create_skill(db_session, "Roof Installation")
    associate_skill(db_session, roofing.id, skill.id)

    profession_response = await client.put(
        "/api/v1/workers/me/professions",
        headers={"X-CSRF-Token": csrf_token},
        json={
            "category_ids": [
                str(construction.id),
                str(electrical.id),
            ]
        },
    )

    assert profession_response.status_code == 200

    skills_response = await client.put(
        f"/api/v1/workers/me/professions/{construction.id}/skills",
        headers={"X-CSRF-Token": csrf_token},
        json={
            "skills": [
                {
                    "category_id": str(roofing.id),
                    "skill_id": str(skill.id),
                }
            ]
        },
    )

    assert skills_response.status_code == 200

    replacement_response = await client.put(
        "/api/v1/workers/me/professions",
        headers={"X-CSRF-Token": csrf_token},
        json={"category_ids": [str(electrical.id)]},
    )

    assert replacement_response.status_code == 200
    assert replacement_response.json()["professions"] == [
        {
            "category_id": str(electrical.id),
            "category_name": "Electrical",
            "skills": [],
        }
    ]

    profile_id = db_session.scalar(select(WorkerProfile.id).where(WorkerProfile.user_id == user.id))

    assert profile_id is not None

    assert (
        db_session.scalar(
            select(WorkerProfession.id).where(
                WorkerProfession.worker_profile_id == profile_id,
                WorkerProfession.category_id == construction.id,
            )
        )
        is None
    )

    assert (
        db_session.scalar(
            select(WorkerSkill.id).where(
                WorkerSkill.worker_profile_id == profile_id,
                WorkerSkill.main_category_id == construction.id,
            )
        )
        is None
    )


async def test_profession_replacement_requires_csrf_token(
    client: AsyncClient,
    db_session: Session,
    valid_password_hash: str,
) -> None:
    user, _ = await create_authenticated_worker(
        client,
        db_session,
        valid_password_hash,
    )

    category = create_category(db_session, "Construction")

    response = await client.put(
        "/api/v1/workers/me/professions",
        json={"category_ids": [str(category.id)]},
    )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "INVALID_CSRF_TOKEN"

    profile_id = db_session.scalar(select(WorkerProfile.id).where(WorkerProfile.user_id == user.id))

    assert profile_id is not None
    assert (
        db_session.scalar(
            select(WorkerProfession.id).where(
                WorkerProfession.worker_profile_id == profile_id,
            )
        )
        is None
    )


async def test_replace_professions_rejects_duplicate_category_ids(
    client: AsyncClient,
    db_session: Session,
    valid_password_hash: str,
) -> None:
    _, csrf_token = await create_authenticated_worker(client, db_session, valid_password_hash)
    construction = create_category(db_session, "Construction")

    response = await client.put(
        "/api/v1/workers/me/professions",
        headers={"X-CSRF-Token": csrf_token},
        json={"category_ids": [str(construction.id), str(construction.id)]},
    )

    assert response.status_code == 422


async def test_empty_profession_selection_clears_existing_professions(
    client: AsyncClient,
    db_session: Session,
    valid_password_hash: str,
) -> None:
    _, csrf_token = await create_authenticated_worker(client, db_session, valid_password_hash)
    construction = create_category(db_session, "Construction")

    initial_response = await client.put(
        "/api/v1/workers/me/professions",
        headers={"X-CSRF-Token": csrf_token},
        json={"category_ids": [str(construction.id)]},
    )
    assert initial_response.status_code == 200

    response = await client.put(
        "/api/v1/workers/me/professions",
        headers={"X-CSRF-Token": csrf_token},
        json={"category_ids": []},
    )

    assert response.status_code == 200
    assert response.json() == {"professions": []}


async def test_empty_skill_selection_clears_existing_skills(
    client: AsyncClient,
    db_session: Session,
    valid_password_hash: str,
) -> None:
    _, csrf_token = await create_authenticated_worker(client, db_session, valid_password_hash)
    construction = create_category(db_session, "Construction")
    roofing = create_category(db_session, "Roofing", parent_id=construction.id)
    skill = create_skill(db_session, "Roof Installation")
    associate_skill(db_session, roofing.id, skill.id)

    profession_response = await client.put(
        "/api/v1/workers/me/professions",
        headers={"X-CSRF-Token": csrf_token},
        json={"category_ids": [str(construction.id)]},
    )
    assert profession_response.status_code == 200

    initial_response = await client.put(
        f"/api/v1/workers/me/professions/{construction.id}/skills",
        headers={"X-CSRF-Token": csrf_token},
        json={"skills": [{"category_id": str(roofing.id), "skill_id": str(skill.id)}]},
    )
    assert initial_response.status_code == 200
    assert len(initial_response.json()["professions"][0]["skills"]) == 1

    response = await client.put(
        f"/api/v1/workers/me/professions/{construction.id}/skills",
        headers={"X-CSRF-Token": csrf_token},
        json={"skills": []},
    )

    assert response.status_code == 200
    assert response.json()["professions"][0]["skills"] == []


async def test_replace_professions_rejects_inactive_category(
    client: AsyncClient,
    db_session: Session,
    valid_password_hash: str,
) -> None:
    _, csrf_token = await create_authenticated_worker(client, db_session, valid_password_hash)
    construction = create_category(db_session, "Construction")
    construction.is_active = False
    db_session.flush()

    response = await client.put(
        "/api/v1/workers/me/professions",
        headers={"X-CSRF-Token": csrf_token},
        json={"category_ids": [str(construction.id)]},
    )

    assert response.status_code == 422


async def test_replace_skills_rejects_inactive_skill(
    client: AsyncClient,
    db_session: Session,
    valid_password_hash: str,
) -> None:
    _, csrf_token = await create_authenticated_worker(client, db_session, valid_password_hash)
    construction = create_category(db_session, "Construction")
    roofing = create_category(db_session, "Roofing", parent_id=construction.id)
    skill = create_skill(db_session, "Roof Installation")
    associate_skill(db_session, roofing.id, skill.id)
    skill.is_active = False
    db_session.flush()

    profession_response = await client.put(
        "/api/v1/workers/me/professions",
        headers={"X-CSRF-Token": csrf_token},
        json={"category_ids": [str(construction.id)]},
    )
    assert profession_response.status_code == 200

    response = await client.put(
        f"/api/v1/workers/me/professions/{construction.id}/skills",
        headers={"X-CSRF-Token": csrf_token},
        json={"skills": [{"category_id": str(roofing.id), "skill_id": str(skill.id)}]},
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "INVALID_WORKER_SELECTION"


async def test_replace_skills_rejects_unassociated_skill(
    client: AsyncClient,
    db_session: Session,
    valid_password_hash: str,
) -> None:
    _, csrf_token = await create_authenticated_worker(client, db_session, valid_password_hash)
    construction = create_category(db_session, "Construction")
    roofing = create_category(db_session, "Roofing", parent_id=construction.id)
    skill = create_skill(db_session, "Roof Installation")

    profession_response = await client.put(
        "/api/v1/workers/me/professions",
        headers={"X-CSRF-Token": csrf_token},
        json={"category_ids": [str(construction.id)]},
    )
    assert profession_response.status_code == 200

    response = await client.put(
        f"/api/v1/workers/me/professions/{construction.id}/skills",
        headers={"X-CSRF-Token": csrf_token},
        json={"skills": [{"category_id": str(roofing.id), "skill_id": str(skill.id)}]},
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "INVALID_WORKER_SELECTION"


async def test_replace_skills_rejects_unselected_profession(
    client: AsyncClient,
    db_session: Session,
    valid_password_hash: str,
) -> None:
    _, csrf_token = await create_authenticated_worker(client, db_session, valid_password_hash)
    construction = create_category(db_session, "Construction")
    roofing = create_category(db_session, "Roofing", parent_id=construction.id)
    skill = create_skill(db_session, "Roof Installation")
    associate_skill(db_session, roofing.id, skill.id)

    response = await client.put(
        f"/api/v1/workers/me/professions/{construction.id}/skills",
        headers={"X-CSRF-Token": csrf_token},
        json={"skills": [{"category_id": str(roofing.id), "skill_id": str(skill.id)}]},
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "INVALID_WORKER_SELECTION"


async def test_retaining_profession_preserves_its_selected_skills(
    client: AsyncClient,
    db_session: Session,
    valid_password_hash: str,
) -> None:
    _, csrf_token = await create_authenticated_worker(client, db_session, valid_password_hash)
    construction = create_category(db_session, "Construction")
    roofing = create_category(db_session, "Roofing", parent_id=construction.id)
    electrical = create_category(db_session, "Electrical")
    plumbing = create_category(db_session, "Plumbing")

    skill = create_skill(db_session, "Roof Installation")
    associate_skill(db_session, roofing.id, skill.id)

    initial_response = await client.put(
        "/api/v1/workers/me/professions",
        headers={"X-CSRF-Token": csrf_token},
        json={"category_ids": [str(construction.id), str(electrical.id)]},
    )
    assert initial_response.status_code == 200

    skill_response = await client.put(
        f"/api/v1/workers/me/professions/{construction.id}/skills",
        headers={"X-CSRF-Token": csrf_token},
        json={"skills": [{"category_id": str(roofing.id), "skill_id": str(skill.id)}]},
    )
    assert skill_response.status_code == 200

    response = await client.put(
        "/api/v1/workers/me/professions",
        headers={"X-CSRF-Token": csrf_token},
        json={"category_ids": [str(construction.id), str(plumbing.id)]},
    )

    assert response.status_code == 200
    professions = {item["category_id"]: item for item in response.json()["professions"]}
    assert set(professions) == {str(construction.id), str(plumbing.id)}
    assert professions[str(construction.id)]["skills"] == [
        {
            "category_id": str(roofing.id),
            "category_name": "Roofing",
            "skill_id": str(skill.id),
            "skill_name": "Roof Installation",
        }
    ]


async def test_invalid_skill_replacement_preserves_existing_selection(
    client: AsyncClient,
    db_session: Session,
    valid_password_hash: str,
) -> None:
    _, csrf_token = await create_authenticated_worker(client, db_session, valid_password_hash)
    construction = create_category(db_session, "Construction")
    roofing = create_category(db_session, "Roofing", parent_id=construction.id)
    electrical = create_category(db_session, "Electrical")

    existing_skill = create_skill(db_session, "Roof Installation")
    replacement_skill = create_skill(db_session, "Roof Repair")
    invalid_skill = create_skill(db_session, "Electrical Wiring")

    associate_skill(db_session, roofing.id, existing_skill.id)
    associate_skill(db_session, roofing.id, replacement_skill.id)
    associate_skill(db_session, electrical.id, invalid_skill.id)

    profession_response = await client.put(
        "/api/v1/workers/me/professions",
        headers={"X-CSRF-Token": csrf_token},
        json={"category_ids": [str(construction.id)]},
    )
    assert profession_response.status_code == 200

    initial_response = await client.put(
        f"/api/v1/workers/me/professions/{construction.id}/skills",
        headers={"X-CSRF-Token": csrf_token},
        json={"skills": [{"category_id": str(roofing.id), "skill_id": str(existing_skill.id)}]},
    )
    assert initial_response.status_code == 200

    response = await client.put(
        f"/api/v1/workers/me/professions/{construction.id}/skills",
        headers={"X-CSRF-Token": csrf_token},
        json={
            "skills": [
                {"category_id": str(roofing.id), "skill_id": str(replacement_skill.id)},
                {"category_id": str(electrical.id), "skill_id": str(invalid_skill.id)},
            ]
        },
    )

    assert response.status_code == 422

    read_response = await client.get("/api/v1/workers/me/professions")
    assert read_response.status_code == 200
    assert read_response.json()["professions"][0]["skills"] == [
        {
            "category_id": str(roofing.id),
            "category_name": "Roofing",
            "skill_id": str(existing_skill.id),
            "skill_name": "Roof Installation",
        }
    ]


async def test_replace_professions_requires_authentication(
    client: AsyncClient,
) -> None:
    response = await client.put(
        "/api/v1/workers/me/professions",
        json={"category_ids": []},
    )

    assert response.status_code == 401


async def test_replace_skills_requires_authentication(
    client: AsyncClient,
) -> None:
    response = await client.put(
        f"/api/v1/workers/me/professions/{uuid4()}/skills",
        json={"skills": []},
    )

    assert response.status_code == 401


async def test_skill_replacement_requires_csrf_token(
    client: AsyncClient,
    db_session: Session,
    valid_password_hash: str,
) -> None:
    _, csrf_token = await create_authenticated_worker(client, db_session, valid_password_hash)
    construction = create_category(db_session, "Construction")

    profession_response = await client.put(
        "/api/v1/workers/me/professions",
        headers={"X-CSRF-Token": csrf_token},
        json={"category_ids": [str(construction.id)]},
    )
    assert profession_response.status_code == 200

    response = await client.put(
        f"/api/v1/workers/me/professions/{construction.id}/skills",
        json={"skills": []},
    )

    assert response.status_code == 403
