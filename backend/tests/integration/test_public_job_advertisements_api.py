from collections.abc import AsyncIterator, Generator
from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.api.dependencies.database import get_db
from app.core.config import settings
from app.main import app
from app.models.identity import User
from app.models.job_advertisement import (
    EmployerPostingCreditEntry,
    Payment,
)
from app.services.employers import create_employer_profile
from app.services.job_advertisements import (
    create_job_advertisement_draft,
    publish_job_advertisement,
)
from app.services.taxonomy import create_category

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


def create_test_employer(
    db: Session,
    *,
    employer_type: str = "individual",
) -> tuple[User, object]:
    """Create an eligible user and employer profile."""
    unique_id = uuid4().hex[:12]

    user = User(
        full_name="Public API Test User",
        username=f"publicapi_{unique_id}",
        username_key=f"publicapi_{unique_id}",
        phone_number=f"+2547{uuid4().int % 100_000_000:08d}",
        password_hash="test-password-hash",
        account_status="active",
        phone_verified_at=datetime.now(UTC),
    )
    db.add(user)
    db.flush()

    employer = create_employer_profile(
        db,
        user.id,
        employer_type=employer_type,
    )

    return user, employer


def grant_posting_credits(
    db: Session,
    user: User,
    employer,
) -> None:
    """Grant five credits through a verified test payment."""
    payment = Payment(
        employer_profile_id=employer.id,
        initiated_by_user_id=user.id,
        purpose="initial_employer_package",
        provider="test",
        provider_environment="sandbox",
        idempotency_key=f"public-api-test-{uuid4()}",
        amount=Decimal("50.00"),
        currency_code="KES",
        status="succeeded",
        verified_at=datetime.now(UTC),
    )
    db.add(payment)
    db.flush()

    db.add(
        EmployerPostingCreditEntry(
            employer_profile_id=employer.id,
            entry_type="initial_grant",
            credit_delta=5,
            payment_id=payment.id,
        )
    )
    db.flush()


def create_published_advertisement(
    db: Session,
    *,
    title: str = "Experienced Electrician",
    description: str = "Electrician needed for residential wiring.",
    employer_type: str = "individual",
    job_type: str = "casual_daily",
):
    """Create and publish an advertisement using real application services."""
    user, employer = create_test_employer(
        db,
        employer_type=employer_type,
    )
    grant_posting_credits(db, user, employer)

    category = create_category(
        db,
        f"Construction {uuid4().hex[:8]}",
    )

    draft = create_job_advertisement_draft(
        db,
        user.id,
        {
            "title": title,
            "description": description,
            "job_type": job_type,
            "work_arrangement": "remote",
            "main_category_id": category.id,
            "compensation_type": "negotiable",
            "workers_needed": 2,
            "contact_methods": ["in_app_message"],
        },
    )

    advertisement = publish_job_advertisement(
        db,
        user.id,
        draft.id,
    )

    return advertisement, employer


async def test_list_public_advertisements_returns_paginated_results(
    client: AsyncClient,
    db_session: Session,
) -> None:
    first, _ = create_published_advertisement(
        db_session,
        title="Electrician required",
    )
    second, _ = create_published_advertisement(
        db_session,
        title="Plumber required",
        job_type="temporary",
    )

    response = await client.get(
        "/api/v1/public/job-advertisements",
        params={"page": 1, "page_size": 1},
    )

    assert response.status_code == 200

    data = response.json()

    assert data["total"] == 2
    assert data["page"] == 1
    assert data["page_size"] == 1
    assert data["pages"] == 2
    assert len(data["items"]) == 1

    returned_ids = {
        first.id,
        second.id,
    }
    assert data["items"][0]["id"] in {str(advertisement_id) for advertisement_id in returned_ids}


async def test_list_public_advertisements_supports_keyword_filter(
    client: AsyncClient,
    db_session: Session,
) -> None:
    electrician, _ = create_published_advertisement(
        db_session,
        title="Electrician required",
    )
    create_published_advertisement(
        db_session,
        title="Plumber required",
        description="Experienced plumber needed.",
    )

    response = await client.get(
        "/api/v1/public/job-advertisements",
        params={"keyword": "electrician"},
    )

    assert response.status_code == 200

    data = response.json()

    assert data["total"] == 1
    assert data["items"][0]["id"] == str(electrician.id)


async def test_list_public_advertisements_supports_job_type_filter(
    client: AsyncClient,
    db_session: Session,
) -> None:
    temporary, _ = create_published_advertisement(
        db_session,
        title="Temporary electrician",
        job_type="temporary",
    )
    create_published_advertisement(
        db_session,
        title="Permanent electrician",
        job_type="full_time_permanent",
    )

    response = await client.get(
        "/api/v1/public/job-advertisements",
        params={"job_type": "temporary"},
    )

    assert response.status_code == 200

    data = response.json()

    assert data["total"] == 1
    assert data["items"][0]["id"] == str(temporary.id)


async def test_public_detail_returns_advertisement(
    client: AsyncClient,
    db_session: Session,
) -> None:
    advertisement, employer = create_published_advertisement(db_session)

    response = await client.get(
        f"/api/v1/public/job-advertisements/{advertisement.id}",
    )

    assert response.status_code == 200

    data = response.json()

    assert data["id"] == str(advertisement.id)
    assert data["title"] == advertisement.title
    assert data["description"] == advertisement.description
    assert data["employer"]["display_name"] == employer.display_name
    assert data["employer"]["employer_type"] == employer.employer_type


async def test_public_detail_does_not_expose_private_information(
    client: AsyncClient,
    db_session: Session,
) -> None:
    advertisement, _ = create_published_advertisement(db_session)

    response = await client.get(
        f"/api/v1/public/job-advertisements/{advertisement.id}",
    )

    assert response.status_code == 200

    data = response.json()
    employer_data = data["employer"]

    assert "user_id" not in employer_data
    assert "phone_number" not in employer_data
    assert "provider_metadata" not in data
    assert "moderation_reason" not in data
    assert "latitude" not in data
    assert "longitude" not in data


async def test_public_detail_hides_draft_advertisements(
    client: AsyncClient,
    db_session: Session,
) -> None:
    user, employer = create_test_employer(db_session)

    draft = create_job_advertisement_draft(
        db_session,
        user.id,
        {"title": "Private draft"},
    )

    response = await client.get(
        f"/api/v1/public/job-advertisements/{draft.id}",
    )

    assert response.status_code == 404
    assert employer.id == draft.employer_profile_id


async def test_public_detail_returns_404_for_unknown_advertisement(
    client: AsyncClient,
) -> None:
    response = await client.get(
        f"/api/v1/public/job-advertisements/{uuid4()}",
    )

    assert response.status_code == 404


@pytest.mark.parametrize(
    "params",
    [
        {"page": 0},
        {"page_size": 0},
        {"page_size": 101},
        {"job_type": "invalid_type"},
        {"work_arrangement": "invalid"},
        {"compensation_type": "invalid"},
        {"employer_type": "invalid"},
        {"sort": "invalid"},
        {"min_compensation": -1},
    ],
)
async def test_invalid_query_parameters_return_422(
    client: AsyncClient,
    params: dict,
) -> None:
    response = await client.get(
        "/api/v1/public/job-advertisements",
        params=params,
    )

    assert response.status_code == 422


async def test_relevance_sorting_without_keyword_returns_422(
    client: AsyncClient,
) -> None:
    response = await client.get(
        "/api/v1/public/job-advertisements",
        params={"sort": "relevance"},
    )

    assert response.status_code == 422


async def test_invalid_compensation_range_returns_422(
    client: AsyncClient,
) -> None:
    response = await client.get(
        "/api/v1/public/job-advertisements",
        params={
            "min_compensation": "2000",
            "max_compensation": "1000",
        },
    )

    assert response.status_code == 422


async def test_invalid_publication_date_range_returns_422(
    client: AsyncClient,
) -> None:
    response = await client.get(
        "/api/v1/public/job-advertisements",
        params={
            "published_after": "2026-11-20",
            "published_before": "2026-11-19",
        },
    )

    assert response.status_code == 422


async def test_invalid_advertisement_uuid_returns_422(
    client: AsyncClient,
) -> None:
    response = await client.get(
        "/api/v1/public/job-advertisements/not-a-uuid",
    )

    assert response.status_code == 422
