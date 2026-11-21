from collections.abc import Generator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
import pytest_asyncio
from fastapi import status
from httpx import ASGITransport, AsyncClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.api.dependencies.database import get_auth_db, get_db
from app.core.config import settings
from app.core.csrf import generate_csrf_token
from app.main import app
from app.models.employer import EmployerProfile
from app.models.identity import User, UserRole
from app.models.job_advertisement import (
    EmployerPostingCreditEntry,
    JobAdvertisement,
    JobAdvertisementAudit,
    Payment,
)
from app.models.taxonomy import Category
from app.services.employers import create_employer_profile
from app.services.job_advertisements import expire_due_job_advertisements
from app.services.sessions import create_session


@pytest.fixture
def test_database() -> Generator[sessionmaker]:
    """Provide an isolated transaction against the PostgreSQL test database."""
    engine = create_engine(
        settings.database_url.set(database="mafundipro_test"),
        pool_pre_ping=True,
    )

    connection = engine.connect()
    transaction = connection.begin()

    factory = sessionmaker(
        bind=connection,
        autoflush=False,
        expire_on_commit=False,
        join_transaction_mode="create_savepoint",
    )

    try:
        yield factory
    finally:
        transaction.rollback()
        connection.close()
        engine.dispose()


@pytest.fixture
def db_session(test_database: sessionmaker) -> Generator[Session]:
    """Provide a database session for test setup."""
    db = test_database()

    try:
        yield db
    finally:
        db.close()


@pytest_asyncio.fixture
async def client(test_database: sessionmaker) -> Generator[AsyncClient]:
    """Exercise the real application using the isolated test database."""

    def override_database() -> Generator[Session]:
        db = test_database()

        try:
            yield db
        finally:
            db.close()

    original_overrides = app.dependency_overrides.copy()

    app.dependency_overrides[get_db] = override_database
    app.dependency_overrides[get_auth_db] = override_database

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
    ) as test_client:
        try:
            yield test_client
        finally:
            app.dependency_overrides.clear()
            app.dependency_overrides.update(original_overrides)


def create_test_employer(
    db: Session,
    *,
    employer_type: str = "individual",
) -> tuple[User, EmployerProfile]:
    """Create an active, phone-verified user with an employer profile."""
    unique_id = uuid4().hex[:12]

    user = User(
        full_name="Employer API Test User",
        username=f"employerapi_{unique_id}",
        username_key=f"employerapi_{unique_id}",
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


def create_test_category(db: Session) -> Category:
    """Create an active root profession category."""
    unique_name = f"Test Profession {uuid4().hex[:10]}"

    category = Category(
        name=unique_name,
        name_key=unique_name.casefold(),
        is_active=True,
    )

    db.add(category)
    db.flush()

    return category


def grant_posting_credits(
    db: Session,
    user: User,
    employer: EmployerProfile,
    *,
    credits: int = 5,
) -> None:
    """Create a verified test payment and its posting-credit ledger entry."""
    payment = Payment(
        employer_profile_id=employer.id,
        initiated_by_user_id=user.id,
        purpose="initial_employer_package",
        provider="test",
        provider_environment="sandbox",
        idempotency_key=f"employer-api-test-{uuid4()}",
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
            credit_delta=credits,
            payment_id=payment.id,
        )
    )
    db.flush()


def authenticate_client(
    client: AsyncClient,
    db: Session,
    user: User,
) -> dict[str, str]:
    """Create a real session and return its session-bound CSRF header."""
    session_token, _ = create_session(db, user.id)
    db.commit()

    csrf_token = generate_csrf_token(session_token)

    client.cookies.set(
        settings.session_cookie_name,
        session_token,
    )

    client.cookies.set(
        settings.csrf_cookie_name,
        csrf_token,
    )

    return {"X-CSRF-Token": csrf_token}


def valid_advertisement_payload(category: Category) -> dict:
    """Return a complete, valid remote advertisement payload."""
    return {
        "title": "Electrician required",
        "description": "An experienced electrician is required.",
        "job_type": "casual_daily",
        "work_arrangement": "remote",
        "main_category_id": str(category.id),
        "compensation_type": "negotiable",
        "workers_needed": 2,
        "contact_methods": ["in_app_message"],
    }


async def create_draft_through_api(
    client: AsyncClient,
    headers: dict[str, str],
    *,
    title: str = "Draft advertisement",
) -> dict:
    """Create a draft through the employer API."""
    response = await client.post(
        "/api/v1/employers/advertisements",
        json={"title": title},
        headers=headers,
    )

    assert response.status_code == status.HTTP_201_CREATED, response.text

    return response.json()


def create_test_platform_user(
    db: Session,
    *,
    role: str | None = None,
) -> User:
    """Create a platform user with an optional moderator or admin role."""
    user, _ = create_test_employer(db)

    if role is not None:
        db.add(UserRole(user_id=user.id, role=role))
        db.flush()

    return user


async def create_published_advertisement(
    client: AsyncClient,
    db: Session,
) -> tuple[User, dict]:
    """Create and publish a valid advertisement through the employer API."""
    owner, employer = create_test_employer(db)
    grant_posting_credits(db, owner, employer)

    category = create_test_category(db)
    headers = authenticate_client(client, db, owner)

    created = await create_draft_through_api(
        client,
        headers,
        title="Moderation test advertisement",
    )

    update_response = await client.patch(
        f"/api/v1/employers/advertisements/{created['id']}",
        json=valid_advertisement_payload(category),
        headers=headers,
    )
    assert update_response.status_code == status.HTTP_200_OK, update_response.text

    publish_response = await client.post(
        f"/api/v1/employers/advertisements/{created['id']}/publish",
        headers=headers,
    )
    assert publish_response.status_code == status.HTTP_200_OK, publish_response.text

    return owner, publish_response.json()


@pytest.mark.asyncio
async def test_employer_advertisement_endpoints_require_authentication(
    client: AsyncClient,
) -> None:
    response = await client.get(
        "/api/v1/employers/advertisements",
    )

    assert response.status_code == status.HTTP_401_UNAUTHORIZED


@pytest.mark.asyncio
async def test_employer_mutations_require_csrf_token(
    client: AsyncClient,
    db_session: Session,
) -> None:
    user, _ = create_test_employer(db_session)

    session_token, _ = create_session(db_session, user.id)
    db_session.commit()

    client.cookies.set(
        settings.session_cookie_name,
        session_token,
    )

    response = await client.post(
        "/api/v1/employers/advertisements",
        json={"title": "Missing CSRF"},
    )

    assert response.status_code == status.HTTP_403_FORBIDDEN
    assert response.json()["error"]["code"] == "INVALID_CSRF_TOKEN"


@pytest.mark.asyncio
async def test_employer_can_create_and_list_drafts(
    client: AsyncClient,
    db_session: Session,
) -> None:
    user, _ = create_test_employer(db_session)
    headers = authenticate_client(client, db_session, user)

    created = await create_draft_through_api(
        client,
        headers,
        title="Electrician draft",
    )

    assert created["title"] == "Electrician draft"
    assert created["status"] == "draft"

    response = await client.get(
        "/api/v1/employers/advertisements",
    )

    assert response.status_code == status.HTTP_200_OK

    data = response.json()

    assert data["total"] == 1
    assert len(data["items"]) == 1
    assert data["items"][0]["id"] == created["id"]


@pytest.mark.asyncio
async def test_employer_can_update_own_draft(
    client: AsyncClient,
    db_session: Session,
) -> None:
    user, _ = create_test_employer(db_session)
    headers = authenticate_client(client, db_session, user)

    created = await create_draft_through_api(
        client,
        headers,
    )

    response = await client.patch(
        f"/api/v1/employers/advertisements/{created['id']}",
        json={"title": "Updated electrician draft"},
        headers=headers,
    )

    assert response.status_code == status.HTTP_200_OK
    assert response.json()["title"] == "Updated electrician draft"


@pytest.mark.asyncio
async def test_employer_cannot_access_another_employers_advertisement(
    client: AsyncClient,
    db_session: Session,
) -> None:
    owner, _ = create_test_employer(db_session)
    other_user, _ = create_test_employer(db_session)

    owner_headers = authenticate_client(client, db_session, owner)

    created = await create_draft_through_api(
        client,
        owner_headers,
    )

    other_headers = authenticate_client(
        client,
        db_session,
        other_user,
    )

    response = await client.get(
        f"/api/v1/employers/advertisements/{created['id']}",
    )

    assert response.status_code == status.HTTP_404_NOT_FOUND

    response = await client.get(
        "/api/v1/employers/advertisements",
        headers=other_headers,
    )

    assert response.status_code == status.HTTP_200_OK
    assert response.json()["total"] == 0

    response = await client.patch(
        f"/api/v1/employers/advertisements/{created['id']}",
        json={"title": "Unauthorized change"},
        headers=other_headers,
    )

    assert response.status_code == status.HTTP_404_NOT_FOUND


@pytest.mark.asyncio
async def test_employer_can_publish_advertisement_and_consume_one_credit(
    client: AsyncClient,
    db_session: Session,
) -> None:
    user, employer = create_test_employer(db_session)
    grant_posting_credits(db_session, user, employer)

    category = create_test_category(db_session)
    headers = authenticate_client(client, db_session, user)

    created = await create_draft_through_api(
        client,
        headers,
    )

    update_response = await client.patch(
        f"/api/v1/employers/advertisements/{created['id']}",
        json=valid_advertisement_payload(category),
        headers=headers,
    )

    assert update_response.status_code == status.HTTP_200_OK

    response = await client.post(
        f"/api/v1/employers/advertisements/{created['id']}/publish",
        headers=headers,
    )

    assert response.status_code == status.HTTP_200_OK, response.text

    published = response.json()

    assert published["status"] == "published"
    assert published["published_at"] is not None
    assert published["expires_at"] is not None

    balance_response = await client.get(
        "/api/v1/employers/advertisements/credits/balance",
    )

    assert balance_response.status_code == status.HTTP_200_OK
    assert balance_response.json()["available_credits"] == 4

    consumption_count = (
        db_session.query(EmployerPostingCreditEntry)
        .filter(
            EmployerPostingCreditEntry.employer_profile_id == employer.id,
            EmployerPostingCreditEntry.entry_type == "consumption",
            EmployerPostingCreditEntry.advertisement_id == published["id"],
        )
        .count()
    )

    assert consumption_count == 1

    advertisement = db_session.get(
        JobAdvertisement,
        published["id"],
    )

    assert advertisement is not None
    assert advertisement.status == "published"


@pytest.mark.asyncio
async def test_publishing_without_credits_does_not_publish_advertisement(
    client: AsyncClient,
    db_session: Session,
) -> None:
    user, _ = create_test_employer(db_session)
    category = create_test_category(db_session)
    headers = authenticate_client(client, db_session, user)

    created = await create_draft_through_api(
        client,
        headers,
    )

    update_response = await client.patch(
        f"/api/v1/employers/advertisements/{created['id']}",
        json=valid_advertisement_payload(category),
        headers=headers,
    )

    assert update_response.status_code == status.HTTP_200_OK

    response = await client.post(
        f"/api/v1/employers/advertisements/{created['id']}/publish",
        headers=headers,
    )

    assert response.status_code == status.HTTP_409_CONFLICT
    assert response.json()["error"]["code"] == "INSUFFICIENT_POSTING_CREDITS"

    detail = await client.get(
        f"/api/v1/employers/advertisements/{created['id']}",
    )

    assert detail.status_code == status.HTTP_200_OK
    assert detail.json()["status"] == "draft"


@pytest.mark.asyncio
async def test_employer_can_update_positions_filled(
    client: AsyncClient,
    db_session: Session,
) -> None:
    user, employer = create_test_employer(db_session)
    grant_posting_credits(db_session, user, employer)

    category = create_test_category(db_session)
    headers = authenticate_client(client, db_session, user)

    created = await create_draft_through_api(
        client,
        headers,
    )

    await client.patch(
        f"/api/v1/employers/advertisements/{created['id']}",
        json=valid_advertisement_payload(category),
        headers=headers,
    )

    publish_response = await client.post(
        f"/api/v1/employers/advertisements/{created['id']}/publish",
        headers=headers,
    )

    assert publish_response.status_code == status.HTTP_200_OK

    response = await client.patch(
        f"/api/v1/employers/advertisements/{created['id']}/positions",
        json={"positions_filled": 1},
        headers=headers,
    )

    assert response.status_code == status.HTTP_200_OK
    assert response.json()["positions_filled"] == 1
    assert response.json()["remaining_positions"] == 1


@pytest.mark.asyncio
async def test_employer_cannot_fill_more_positions_than_needed(
    client: AsyncClient,
    db_session: Session,
) -> None:
    user, employer = create_test_employer(db_session)
    grant_posting_credits(db_session, user, employer)

    category = create_test_category(db_session)
    headers = authenticate_client(client, db_session, user)

    created = await create_draft_through_api(
        client,
        headers,
    )

    update_response = await client.patch(
        f"/api/v1/employers/advertisements/{created['id']}",
        json=valid_advertisement_payload(category),
        headers=headers,
    )

    assert update_response.status_code == status.HTTP_200_OK

    publish_response = await client.post(
        f"/api/v1/employers/advertisements/{created['id']}/publish",
        headers=headers,
    )

    assert publish_response.status_code == status.HTTP_200_OK

    response = await client.patch(
        f"/api/v1/employers/advertisements/{created['id']}/positions",
        json={"positions_filled": 3},
        headers=headers,
    )

    assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT


@pytest.mark.asyncio
async def test_employer_can_close_own_published_advertisement(
    client: AsyncClient,
    db_session: Session,
) -> None:
    user, employer = create_test_employer(db_session)
    grant_posting_credits(db_session, user, employer)

    category = create_test_category(db_session)
    headers = authenticate_client(client, db_session, user)

    created = await create_draft_through_api(
        client,
        headers,
    )

    await client.patch(
        f"/api/v1/employers/advertisements/{created['id']}",
        json=valid_advertisement_payload(category),
        headers=headers,
    )

    publish_response = await client.post(
        f"/api/v1/employers/advertisements/{created['id']}/publish",
        headers=headers,
    )

    assert publish_response.status_code == status.HTTP_200_OK

    response = await client.post(
        f"/api/v1/employers/advertisements/{created['id']}/close",
        headers=headers,
    )

    assert response.status_code == status.HTTP_200_OK
    assert response.json()["status"] == "closed"
    assert response.json()["closed_at"] is not None


@pytest.mark.asyncio
async def test_employer_credit_balance_reports_configured_prices(
    client: AsyncClient,
    db_session: Session,
) -> None:
    user, employer = create_test_employer(db_session)
    grant_posting_credits(db_session, user, employer)

    headers = authenticate_client(client, db_session, user)

    response = await client.get(
        "/api/v1/employers/advertisements/credits/balance",
        headers=headers,
    )

    assert response.status_code == status.HTTP_200_OK

    data = response.json()

    assert data["available_credits"] == 5
    assert data["initial_package_credits"] == 5
    assert Decimal(str(data["initial_package_price"])) == Decimal("50.00")
    assert Decimal(str(data["additional_credit_price"])) == Decimal("50.00")
    assert data["currency_code"] == "KES"


@pytest.mark.asyncio
async def test_employer_advertisement_list_supports_pagination(
    client: AsyncClient,
    db_session: Session,
) -> None:
    user, _ = create_test_employer(db_session)
    headers = authenticate_client(client, db_session, user)

    for index in range(3):
        await create_draft_through_api(
            client,
            headers,
            title=f"Draft {index}",
        )

    response = await client.get(
        "/api/v1/employers/advertisements",
        params={"page": 1, "page_size": 2},
    )

    assert response.status_code == status.HTTP_200_OK

    data = response.json()

    assert data["total"] == 3
    assert data["page"] == 1
    assert data["page_size"] == 2
    assert len(data["items"]) == 2
    assert data["pages"] == 2


@pytest.mark.asyncio
async def test_moderator_can_suspend_and_reinstate_advertisement(
    client: AsyncClient,
    db_session: Session,
) -> None:
    owner, published = await create_published_advertisement(client, db_session)
    moderator = create_test_platform_user(db_session, role="moderator")

    advertisement_id = published["id"]
    original_expiry = published["expires_at"]

    moderator_headers = authenticate_client(client, db_session, moderator)

    suspend_response = await client.post(
        f"/api/v1/admin/job-advertisements/{advertisement_id}/suspend",
        json={"reason": "The advertisement requires verification."},
        headers=moderator_headers,
    )

    assert suspend_response.status_code == status.HTTP_200_OK, suspend_response.text
    assert suspend_response.json()["status"] == "suspended"
    assert suspend_response.json()["reason"] == "The advertisement requires verification."

    public_response = await client.get(
        f"/api/v1/public/job-advertisements/{advertisement_id}",
    )
    assert public_response.status_code == status.HTTP_404_NOT_FOUND

    owner_headers = authenticate_client(client, db_session, owner)

    employer_response = await client.get(
        f"/api/v1/employers/advertisements/{advertisement_id}",
        headers=owner_headers,
    )

    assert employer_response.status_code == status.HTTP_200_OK
    assert employer_response.json()["status"] == "suspended"
    assert (
        employer_response.json()["moderation_reason"] == "The advertisement requires verification."
    )

    # Authentication changes the shared client's cookies, so restore the
    # moderator session before making the reinstatement request.
    moderator_headers = authenticate_client(client, db_session, moderator)

    reinstate_response = await client.post(
        f"/api/v1/admin/job-advertisements/{advertisement_id}/reinstate",
        json={"reason": "The employer resolved the verification issue."},
        headers=moderator_headers,
    )

    assert reinstate_response.status_code == status.HTTP_200_OK, reinstate_response.text
    assert reinstate_response.json()["status"] == "published"
    assert reinstate_response.json()["expires_at"] == original_expiry

    public_response = await client.get(
        f"/api/v1/public/job-advertisements/{advertisement_id}",
    )
    assert public_response.status_code == status.HTTP_200_OK

    audit_entries = (
        db_session.query(JobAdvertisementAudit)
        .filter(JobAdvertisementAudit.advertisement_id == advertisement_id)
        .all()
    )

    moderation_entries = {
        entry.action: entry for entry in audit_entries if entry.action in {"suspend", "reinstate"}
    }

    assert set(moderation_entries) == {"suspend", "reinstate"}
    assert all(entry.actor_user_id == moderator.id for entry in moderation_entries.values())
    assert moderation_entries["suspend"].reason == "The advertisement requires verification."
    assert moderation_entries["reinstate"].reason == "The employer resolved the verification issue."


@pytest.mark.asyncio
async def test_only_admin_can_remove_advertisement(
    client: AsyncClient,
    db_session: Session,
) -> None:
    owner, published = await create_published_advertisement(client, db_session)

    moderator = create_test_platform_user(db_session, role="moderator")
    administrator = create_test_platform_user(db_session, role="admin")

    advertisement_id = published["id"]

    moderator_headers = authenticate_client(client, db_session, moderator)

    moderator_response = await client.post(
        f"/api/v1/admin/job-advertisements/{advertisement_id}/remove",
        json={"reason": "Moderator removal attempt."},
        headers=moderator_headers,
    )

    assert moderator_response.status_code == status.HTTP_403_FORBIDDEN

    admin_headers = authenticate_client(client, db_session, administrator)

    removal_response = await client.post(
        f"/api/v1/admin/job-advertisements/{advertisement_id}/remove",
        json={"reason": "This advertisement violates platform policy."},
        headers=admin_headers,
    )

    assert removal_response.status_code == status.HTTP_200_OK, removal_response.text
    assert removal_response.json()["status"] == "removed"
    assert removal_response.json()["reason"] == "This advertisement violates platform policy."

    owner_headers = authenticate_client(client, db_session, owner)

    employer_response = await client.get(
        f"/api/v1/employers/advertisements/{advertisement_id}",
        headers=owner_headers,
    )

    assert employer_response.status_code == status.HTTP_200_OK
    assert employer_response.json()["status"] == "removed"
    assert (
        employer_response.json()["moderation_reason"]
        == "This advertisement violates platform policy."
    )

    public_response = await client.get(
        f"/api/v1/public/job-advertisements/{advertisement_id}",
    )
    assert public_response.status_code == status.HTTP_404_NOT_FOUND

    # Restore the administrator's session and CSRF cookie after the
    # employer request before testing the rejected reinstatement.
    admin_headers = authenticate_client(client, db_session, administrator)

    reinstate_response = await client.post(
        f"/api/v1/admin/job-advertisements/{advertisement_id}/reinstate",
        json={"reason": "Attempt to reinstate removed advertisement."},
        headers=admin_headers,
    )

    assert reinstate_response.status_code == status.HTTP_409_CONFLICT

    removal_audit = (
        db_session.query(JobAdvertisementAudit)
        .filter(
            JobAdvertisementAudit.advertisement_id == advertisement_id,
            JobAdvertisementAudit.action == "remove",
        )
        .one()
    )

    assert removal_audit.actor_user_id == administrator.id
    assert removal_audit.reason == "This advertisement violates platform policy."


@pytest.mark.asyncio
async def test_users_without_moderation_roles_cannot_moderate(
    client: AsyncClient,
    db_session: Session,
) -> None:
    _, published = await create_published_advertisement(client, db_session)
    ordinary_user = create_test_platform_user(db_session)
    headers = authenticate_client(client, db_session, ordinary_user)

    advertisement_id = published["id"]

    for action in ("suspend", "reinstate", "remove"):
        response = await client.post(
            f"/api/v1/admin/job-advertisements/{advertisement_id}/{action}",
            json={"reason": "Unauthorized moderation attempt."},
            headers=headers,
        )

        assert response.status_code == status.HTTP_403_FORBIDDEN, (
            action,
            response.text,
        )


@pytest.mark.asyncio
async def test_moderation_requires_a_nonempty_reason(
    client: AsyncClient,
    db_session: Session,
) -> None:
    _, published = await create_published_advertisement(client, db_session)
    moderator = create_test_platform_user(db_session, role="moderator")
    headers = authenticate_client(client, db_session, moderator)

    response = await client.post(
        f"/api/v1/admin/job-advertisements/{published['id']}/suspend",
        json={"reason": "   "},
        headers=headers,
    )

    assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT


@pytest.mark.asyncio
async def test_expired_advertisement_leaves_published_and_public_views(
    client: AsyncClient,
    db_session: Session,
) -> None:
    owner, published = await create_published_advertisement(client, db_session)
    advertisement_id = published["id"]
    headers = authenticate_client(client, db_session, owner)

    before = await client.get(f"/api/v1/public/job-advertisements/{advertisement_id}")
    assert before.status_code == status.HTTP_200_OK

    expired_count = expire_due_job_advertisements(
        db_session,
        now=datetime.now(UTC) + timedelta(days=365),
    )
    db_session.commit()
    assert expired_count == 1

    published_list = await client.get(
        "/api/v1/employers/advertisements",
        params={"status": "published"},
        headers=headers,
    )
    assert published_list.status_code == status.HTTP_200_OK
    assert advertisement_id not in [item["id"] for item in published_list.json()["items"]]

    expired_list = await client.get(
        "/api/v1/employers/advertisements",
        params={"status": "expired"},
        headers=headers,
    )
    assert expired_list.status_code == status.HTTP_200_OK
    expired_items = expired_list.json()["items"]
    assert [item["id"] for item in expired_items] == [advertisement_id]
    assert expired_items[0]["status"] == "expired"

    public_detail = await client.get(f"/api/v1/public/job-advertisements/{advertisement_id}")
    assert public_detail.status_code == status.HTTP_404_NOT_FOUND

    public_search = await client.get("/api/v1/public/job-advertisements")
    assert public_search.status_code == status.HTTP_200_OK
    assert advertisement_id not in [item["id"] for item in public_search.json()["items"]]
