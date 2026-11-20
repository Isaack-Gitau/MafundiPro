from collections.abc import AsyncIterator, Generator
from uuid import uuid4

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import create_engine, delete
from sqlalchemy.orm import Session, sessionmaker

from app.api.dependencies.database import get_db
from app.core.config import settings
from app.main import app
from app.models.location import Location
from app.services.locations import create_location, set_location_active

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


async def test_public_location_tree_hides_inactive_ancestor_branches_and_coordinates(
    client: AsyncClient,
    db_session: Session,
) -> None:
    marker = uuid4().hex[:12]

    country = create_location(
        db_session,
        "country",
        f"Kenya {marker}",
        country_code="KE",
        latitude=-1.286389,
        longitude=36.817223,
    )
    active_county = create_location(
        db_session,
        "county",
        f"Nairobi {marker}",
        parent_id=country.id,
        latitude=-1.286389,
        longitude=36.817223,
    )
    sub_county = create_location(
        db_session,
        "sub_county",
        f"Westlands {marker}",
        parent_id=active_county.id,
    )
    estate = create_location(
        db_session,
        "estate",
        f"Parklands {marker}",
        parent_id=sub_county.id,
    )

    inactive_county = create_location(
        db_session,
        "county",
        f"Hidden County {marker}",
        parent_id=country.id,
    )
    hidden_town = create_location(
        db_session,
        "town",
        f"Hidden Town {marker}",
        parent_id=inactive_county.id,
    )
    hidden_area = create_location(
        db_session,
        "area",
        f"Hidden Area {marker}",
        parent_id=hidden_town.id,
    )

    set_location_active(db_session, inactive_county.id, False)
    db_session.commit()

    db_session.refresh(inactive_county)
    assert inactive_county.is_active is False

    location_ids = [
        country.id,
        active_county.id,
        sub_county.id,
        estate.id,
        inactive_county.id,
        hidden_town.id,
        hidden_area.id,
    ]

    try:
        response = await client.get("/api/v1/public/locations")

        assert response.status_code == 200

        locations = response.json()
        country_result = next(item for item in locations if item["id"] == str(country.id))

        county_result = next(
            item for item in country_result["children"] if item["id"] == str(active_county.id)
        )
        sub_county_result = county_result["children"][0]

        assert sub_county_result["id"] == str(sub_county.id)
        assert sub_county_result["children"][0]["id"] == str(estate.id)

        returned_ids = set()

        def collect_ids(items: list[dict]) -> None:
            for item in items:
                returned_ids.add(item["id"])
                collect_ids(item["children"])

        collect_ids(locations)

        expected_visible_ids = {
            str(country.id),
            str(active_county.id),
            str(sub_county.id),
            str(estate.id),
        }

        assert expected_visible_ids <= returned_ids

        assert str(inactive_county.id) not in returned_ids
        assert str(hidden_town.id) not in returned_ids
        assert str(hidden_area.id) not in returned_ids

        assert "latitude" not in response.text
        assert "longitude" not in response.text

    finally:
        for location_id in reversed(location_ids):
            db_session.execute(delete(Location).where(Location.id == location_id))
        db_session.commit()
