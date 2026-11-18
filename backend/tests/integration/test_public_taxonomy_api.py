from collections.abc import AsyncIterator, Generator
from uuid import uuid4

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete
from sqlalchemy.orm import Session

from app.api.dependencies.database import get_db
from app.database.session import SessionLocal
from app.main import app
from app.models.taxonomy import Category, Skill, category_skills
from app.services.taxonomy import (
    associate_skill,
    create_category,
    create_skill,
    set_category_active,
    set_skill_active,
)

pytestmark = pytest.mark.asyncio


@pytest.fixture
def db_session() -> Generator[Session]:
    session = SessionLocal()

    try:
        yield session
    finally:
        session.rollback()
        session.close()


@pytest_asyncio.fixture
async def client() -> AsyncIterator[AsyncClient]:
    previous_overrides = app.dependency_overrides.copy()

    def override_get_db() -> Generator[Session]:
        with SessionLocal() as session:
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

    category_ids = [
        construction.id,
        roofing.id,
        maintenance.id,
        empty_category.id,
        hidden_parent.id,
        hidden_child.id,
        hidden_grandchild.id,
    ]
    skill_ids = [shared_skill.id, inactive_skill.id]

    try:
        response = await client.get("/api/v1/public/categories")

        assert response.status_code == 200

        categories = response.json()
        returned_names = {category["name"] for category in categories}

        assert returned_names == {
            construction.name,
            maintenance.name,
            empty_category.name,
        }

        construction_result = next(
            item for item in categories if item["id"] == str(construction.id)
        )

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

        returned_skill_ids = {
            skill["id"] for category in categories for skill in category["skills"]
        }

        assert str(inactive_skill.id) not in returned_skill_ids

    finally:
        db_session.execute(
            delete(category_skills).where(category_skills.c.category_id.in_(category_ids))
        )

        db_session.execute(
            delete(Category).where(
                Category.id.in_(
                    [
                        hidden_grandchild.id,
                        roofing.id,
                        hidden_child.id,
                    ]
                )
            )
        )

        db_session.execute(
            delete(Category).where(
                Category.id.in_(
                    [
                        construction.id,
                        maintenance.id,
                        empty_category.id,
                        hidden_parent.id,
                    ]
                )
            )
        )

        db_session.execute(delete(Skill).where(Skill.id.in_(skill_ids)))

        db_session.commit()
