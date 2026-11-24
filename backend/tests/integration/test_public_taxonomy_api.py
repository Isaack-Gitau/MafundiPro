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
from app.services.taxonomy import (
    associate_skill,
    create_category,
    create_skill,
    set_category_active,
    set_skill_active,
)

pytestmark = pytest.mark.asyncio


@pytest.fixture
def test_database() -> Generator[sessionmaker]:
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
def db_session(test_database: sessionmaker) -> Generator[Session]:
    """Provide a session for preparing test data."""
    with test_database() as session:
        yield session


@pytest_asyncio.fixture
async def client(test_database: sessionmaker) -> AsyncIterator[AsyncClient]:
    """Provide an HTTP client using the isolated PostgreSQL test database."""
    previous_overrides = app.dependency_overrides.copy()

    def override_get_db() -> Generator[Session]:
        with test_database() as session:
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


async def test_public_category_tree_filters_inactive_records(
    client: AsyncClient,
    db_session: Session,
) -> None:
    marker = uuid4().hex[:12]

    construction = create_category(
        db_session,
        f"Construction {marker}",
    )
    roofing = create_category(
        db_session,
        f"Roofing {marker}",
        parent_id=construction.id,
    )

    maintenance = create_category(
        db_session,
        f"Maintenance {marker}",
    )

    empty_category = create_category(
        db_session,
        f"Empty Category {marker}",
    )

    hidden_parent = create_category(
        db_session,
        f"Hidden Parent {marker}",
    )
    hidden_child = create_category(
        db_session,
        f"Hidden Child {marker}",
        parent_id=hidden_parent.id,
    )
    hidden_grandchild = create_category(
        db_session,
        f"Hidden Grandchild {marker}",
        parent_id=hidden_child.id,
    )

    shared_skill = create_skill(
        db_session,
        f"Installation {marker}",
    )
    inactive_skill = create_skill(
        db_session,
        f"Inactive Skill {marker}",
    )

    associate_skill(db_session, construction.id, shared_skill.id)
    associate_skill(db_session, maintenance.id, shared_skill.id)
    associate_skill(db_session, construction.id, inactive_skill.id)

    set_skill_active(db_session, inactive_skill.id, False)
    set_category_active(db_session, hidden_parent.id, False)

    db_session.commit()

    response = await client.get("/api/v1/public/categories")

    assert response.status_code == 200

    categories = response.json()
    returned_names = {category["name"] for category in categories}

    assert returned_names == {
        construction.name,
        maintenance.name,
        empty_category.name,
    }

    construction_result = next(item for item in categories if item["id"] == str(construction.id))

    assert [child["name"] for child in construction_result["children"]] == [roofing.name]

    assert [skill["name"] for skill in construction_result["skills"]] == [shared_skill.name]

    maintenance_result = next(item for item in categories if item["id"] == str(maintenance.id))

    assert [skill["name"] for skill in maintenance_result["skills"]] == [shared_skill.name]

    empty_result = next(item for item in categories if item["id"] == str(empty_category.id))
    assert empty_result["skills"] == []
    assert empty_result["children"] == []

    all_returned_ids = {category["id"] for category in categories}

    all_returned_ids.update(
        child["id"] for category in categories for child in category["children"]
    )

    assert str(hidden_parent.id) not in all_returned_ids
    assert str(hidden_child.id) not in all_returned_ids
    assert str(hidden_grandchild.id) not in all_returned_ids

    returned_skill_ids = {skill["id"] for category in categories for skill in category["skills"]}

    assert str(inactive_skill.id) not in returned_skill_ids
