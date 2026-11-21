from collections.abc import Generator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy.orm import Session

from app.database.session import engine
from app.models.employer import EmployerProfile
from app.models.identity import User
from app.models.job_advertisement import JobAdvertisement
from app.services.employers import create_employer_profile
from app.services.job_advertisements import (
    create_job_advertisement_draft,
    publish_job_advertisement,
    update_job_advertisement_positions_filled,
)
from app.services.locations import create_location
from app.services.public_job_advertisements import (
    get_public_job_advertisement,
    search_public_job_advertisements,
)
from app.services.taxonomy import associate_skill, create_category, create_skill


@pytest.fixture
def db_session() -> Generator[Session]:
    """Provide an isolated PostgreSQL transaction for each test."""
    with engine.connect() as connection:
        transaction = connection.begin()
        session = Session(
            bind=connection,
            autoflush=False,
            expire_on_commit=False,
        )

        try:
            yield session
        finally:
            session.close()
            transaction.rollback()


def create_test_employer(
    db: Session,
    *,
    employer_type: str = "individual",
) -> tuple[User, EmployerProfile]:
    """Create an eligible user and employer profile."""
    unique_id = uuid4().hex[:12]

    user = User(
        full_name="Public Advertisement Test User",
        username=f"publicad_{unique_id}",
        username_key=f"publicad_{unique_id}",
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
    employer: EmployerProfile,
) -> None:
    """Grant five posting credits through the verified test payment workflow."""
    from app.models.job_advertisement import (
        EmployerPostingCreditEntry,
        Payment,
    )

    payment = Payment(
        employer_profile_id=employer.id,
        initiated_by_user_id=user.id,
        purpose="initial_employer_package",
        provider="test",
        provider_environment="sandbox",
        idempotency_key=f"public-test-{uuid4()}",
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
    employer_type: str = "individual",
    title: str = "Roof Repair Specialist",
    description: str = "Experienced roofer needed for residential repairs.",
    job_type: str = "casual_daily",
    work_arrangement: str = "remote",
    compensation_type: str = "negotiable",
    min_compensation: Decimal | None = None,
    max_compensation: Decimal | None = None,
    payment_unit: str | None = None,
    workers_needed: int = 2,
    contact_methods: list[str] | None = None,
    response_deadline: datetime | None = None,
    category_id=None,
    subcategory_id=None,
    skill_ids: list | None = None,
    location_id=None,
) -> tuple[JobAdvertisement, User, EmployerProfile]:
    """Create and publish a valid advertisement using real services."""
    user, employer = create_test_employer(
        db,
        employer_type=employer_type,
    )
    grant_posting_credits(db, user, employer)

    if category_id is None:
        category = create_category(
            db,
            f"Construction {uuid4().hex[:8]}",
        )
        category_id = category.id

    values = {
        "title": title,
        "description": description,
        "job_type": job_type,
        "work_arrangement": work_arrangement,
        "main_category_id": category_id,
        "subcategory_id": subcategory_id,
        "compensation_type": compensation_type,
        "min_compensation": min_compensation,
        "max_compensation": max_compensation,
        "payment_unit": payment_unit,
        "workers_needed": workers_needed,
        "contact_methods": contact_methods or ["in_app_message"],
        "response_deadline": response_deadline,
        "location_id": location_id,
    }

    if skill_ids:
        values["skill_ids"] = skill_ids

    advertisement = create_job_advertisement_draft(
        db,
        user.id,
        values,
    )

    published = publish_job_advertisement(
        db,
        user.id,
        advertisement.id,
    )

    return published, user, employer


def test_public_search_returns_published_unexpired_advertisements(
    db_session: Session,
) -> None:
    published, _, _ = create_published_advertisement(db_session)

    results = search_public_job_advertisements(db_session)

    assert results.total == 1
    assert len(results.items) == 1
    assert results.items[0].id == published.id


def test_public_search_excludes_drafts(
    db_session: Session,
) -> None:
    user, employer = create_test_employer(db_session)

    draft = create_job_advertisement_draft(
        db_session,
        user.id,
        {"title": "Private Draft"},
    )

    results = search_public_job_advertisements(db_session)

    assert results.total == 0
    assert draft.status == "draft"
    assert employer.id == draft.employer_profile_id


def test_public_search_excludes_expired_advertisements(
    db_session: Session,
) -> None:
    advertisement, _, _ = create_published_advertisement(db_session)

    now = datetime.now(UTC)
    advertisement.published_at = now - timedelta(seconds=2)
    advertisement.expires_at = now - timedelta(seconds=1)
    db_session.flush()

    results = search_public_job_advertisements(db_session)

    assert results.total == 0
    assert (
        get_public_job_advertisement(
            db_session,
            advertisement.id,
        )
        is None
    )


def test_public_detail_returns_only_published_advertisements(
    db_session: Session,
) -> None:
    advertisement, _, _ = create_published_advertisement(db_session)

    result = get_public_job_advertisement(
        db_session,
        advertisement.id,
    )

    assert result is not None
    assert result.id == advertisement.id
    assert result.title == advertisement.title


def test_public_detail_returns_none_for_unknown_advertisement(
    db_session: Session,
) -> None:
    assert get_public_job_advertisement(db_session, uuid4()) is None


def test_public_response_exposes_only_safe_employer_information(
    db_session: Session,
) -> None:
    advertisement, _, employer = create_published_advertisement(db_session)

    result = get_public_job_advertisement(
        db_session,
        advertisement.id,
    )

    assert result is not None
    assert result.employer.display_name == employer.display_name
    assert result.employer.employer_type == employer.employer_type

    response_data = result.model_dump()

    assert "phone_number" not in response_data
    assert "user_id" not in response_data
    assert "provider_metadata" not in response_data
    assert "moderation_reason" not in response_data


def test_fixed_compensation_is_exposed(
    db_session: Session,
) -> None:
    advertisement, _, _ = create_published_advertisement(
        db_session,
        compensation_type="fixed",
        min_compensation=Decimal("1000.00"),
        max_compensation=Decimal("2000.00"),
        payment_unit="daily",
    )

    result = get_public_job_advertisement(
        db_session,
        advertisement.id,
    )

    assert result is not None
    assert result.min_compensation == Decimal("1000.00")
    assert result.max_compensation == Decimal("2000.00")
    assert result.payment_unit == "daily"
    assert result.currency_code == "KES"


@pytest.mark.parametrize(
    "status",
    ["suspended", "removed", "closed"],
)
def test_public_search_and_detail_exclude_unavailable_statuses(
    db_session: Session,
    status: str,
) -> None:
    advertisement, _, _ = create_published_advertisement(db_session)

    advertisement.status = status
    db_session.flush()

    results = search_public_job_advertisements(db_session)

    assert results.total == 0
    assert all(item.id != advertisement.id for item in results.items)
    assert (
        get_public_job_advertisement(
            db_session,
            advertisement.id,
        )
        is None
    )


@pytest.mark.parametrize("compensation_type", ["negotiable", "undisclosed"])
def test_non_fixed_compensation_hides_amounts_and_payment_unit(
    db_session: Session,
    compensation_type: str,
) -> None:
    advertisement, _, _ = create_published_advertisement(
        db_session,
        compensation_type=compensation_type,
    )

    result = get_public_job_advertisement(
        db_session,
        advertisement.id,
    )

    assert result is not None
    assert result.compensation_type == compensation_type
    assert result.min_compensation is None
    assert result.max_compensation is None
    assert result.payment_unit is None
    assert result.currency_code is None


def test_contact_methods_are_hidden_when_all_positions_are_filled(
    db_session: Session,
) -> None:
    advertisement, user, _ = create_published_advertisement(db_session)

    update_job_advertisement_positions_filled(
        db_session,
        user.id,
        advertisement.id,
        2,
    )

    result = get_public_job_advertisement(
        db_session,
        advertisement.id,
    )

    assert result is not None
    assert result.remaining_positions == 0
    assert result.available_contact_methods == []


def test_contact_methods_are_hidden_after_response_deadline(
    db_session: Session,
) -> None:
    advertisement, _, _ = create_published_advertisement(
        db_session,
        response_deadline=datetime.now(UTC) + timedelta(days=1),
    )

    advertisement.response_deadline = datetime.now(UTC) - timedelta(seconds=1)
    db_session.flush()

    result = get_public_job_advertisement(
        db_session,
        advertisement.id,
    )

    assert result is not None
    assert result.available_contact_methods == []


def test_contact_methods_return_when_positions_are_reopened_before_deadline(
    db_session: Session,
) -> None:
    advertisement, user, _ = create_published_advertisement(
        db_session,
        response_deadline=datetime.now(UTC) + timedelta(days=1),
    )

    update_job_advertisement_positions_filled(
        db_session,
        user.id,
        advertisement.id,
        2,
    )

    update_job_advertisement_positions_filled(
        db_session,
        user.id,
        advertisement.id,
        1,
    )

    result = get_public_job_advertisement(
        db_session,
        advertisement.id,
    )

    assert result is not None
    assert result.remaining_positions == 1
    assert result.available_contact_methods == ["in_app_message"]


def test_keyword_search_matches_title(
    db_session: Session,
) -> None:
    advertisement, _, _ = create_published_advertisement(
        db_session,
        title="Experienced Electrician",
    )

    results = search_public_job_advertisements(
        db_session,
        keyword="electrician",
    )

    assert results.total == 1
    assert results.items[0].id == advertisement.id


def test_keyword_search_matches_description(
    db_session: Session,
) -> None:
    advertisement, _, _ = create_published_advertisement(
        db_session,
        description="Urgently looking for a qualified plumber.",
    )

    results = search_public_job_advertisements(
        db_session,
        keyword="qualified plumber",
    )

    assert results.total == 1
    assert results.items[0].id == advertisement.id


def test_filters_by_job_type_and_work_arrangement(
    db_session: Session,
) -> None:
    advertisement, _, _ = create_published_advertisement(
        db_session,
        job_type="temporary",
        work_arrangement="remote",
    )

    results = search_public_job_advertisements(
        db_session,
        job_type="temporary",
        work_arrangement="remote",
    )

    assert results.total == 1
    assert results.items[0].id == advertisement.id

    assert (
        search_public_job_advertisements(
            db_session,
            job_type="full_time_permanent",
        ).total
        == 0
    )


def test_filters_by_employer_type(
    db_session: Session,
) -> None:
    advertisement, _, _ = create_published_advertisement(
        db_session,
        employer_type="business",
    )

    results = search_public_job_advertisements(
        db_session,
        employer_type="business",
    )

    assert results.total == 1
    assert results.items[0].id == advertisement.id


def test_filters_by_fixed_compensation_range(
    db_session: Session,
) -> None:
    advertisement, _, _ = create_published_advertisement(
        db_session,
        compensation_type="fixed",
        min_compensation=Decimal("1000.00"),
        max_compensation=Decimal("2000.00"),
        payment_unit="daily",
    )

    results = search_public_job_advertisements(
        db_session,
        min_compensation=Decimal("1500.00"),
        max_compensation=Decimal("2500.00"),
    )

    assert results.total == 1
    assert results.items[0].id == advertisement.id

    assert (
        search_public_job_advertisements(
            db_session,
            min_compensation=Decimal("2500.00"),
        ).total
        == 0
    )


def test_available_positions_filter_excludes_filled_advertisements(
    db_session: Session,
) -> None:
    advertisement, user, _ = create_published_advertisement(db_session)

    update_job_advertisement_positions_filled(
        db_session,
        user.id,
        advertisement.id,
        2,
    )

    results = search_public_job_advertisements(
        db_session,
        available_positions_only=True,
    )

    assert results.total == 0


def test_pagination_reports_total_and_pages(
    db_session: Session,
) -> None:
    for index in range(3):
        create_published_advertisement(
            db_session,
            title=f"Pagination Test Advertisement {index}",
        )

    first_page = search_public_job_advertisements(
        db_session,
        page=1,
        page_size=2,
    )
    second_page = search_public_job_advertisements(
        db_session,
        page=2,
        page_size=2,
    )

    assert first_page.total == 3
    assert first_page.pages == 2
    assert len(first_page.items) == 2

    assert second_page.total == 3
    assert second_page.pages == 2
    assert len(second_page.items) == 1


@pytest.mark.parametrize(
    ("page", "page_size"),
    [(0, 20), (1, 0), (1, 101)],
)
def test_invalid_pagination_is_rejected(
    db_session: Session,
    page: int,
    page_size: int,
) -> None:
    with pytest.raises(ValueError):
        search_public_job_advertisements(
            db_session,
            page=page,
            page_size=page_size,
        )


def test_relevance_sorting_requires_a_keyword(
    db_session: Session,
) -> None:
    with pytest.raises(ValueError):
        search_public_job_advertisements(
            db_session,
            sort="relevance",
        )


def test_invalid_compensation_range_is_rejected(
    db_session: Session,
) -> None:
    with pytest.raises(ValueError):
        search_public_job_advertisements(
            db_session,
            min_compensation=Decimal("2000.00"),
            max_compensation=Decimal("1000.00"),
        )


def test_location_filter_includes_descendant_locations(
    db_session: Session,
) -> None:
    country = create_location(
        db_session,
        "country",
        "Kenya",
        country_code="KE",
    )
    county = create_location(
        db_session,
        "county",
        f"Test County {uuid4().hex[:8]}",
        parent_id=country.id,
    )
    town = create_location(
        db_session,
        "town",
        f"Test Town {uuid4().hex[:8]}",
        parent_id=county.id,
    )

    advertisement, _, _ = create_published_advertisement(
        db_session,
        work_arrangement="on_site",
        location_id=town.id,
    )

    results = search_public_job_advertisements(
        db_session,
        location_id=county.id,
    )

    assert results.total == 1
    assert results.items[0].id == advertisement.id
    assert results.items[0].location is not None
    assert [entry.name for entry in results.items[0].location.path] == [
        "Kenya",
        county.name,
        town.name,
    ]


def test_skill_filter_returns_advertisements_requiring_skill(
    db_session: Session,
) -> None:
    profession = create_category(
        db_session,
        f"Construction {uuid4().hex[:8]}",
    )
    subcategory = create_category(
        db_session,
        f"Roofing {uuid4().hex[:8]}",
        parent_id=profession.id,
    )
    skill = create_skill(
        db_session,
        f"Roof installation {uuid4().hex[:8]}",
    )
    associate_skill(db_session, subcategory.id, skill.id)

    advertisement, _, _ = create_published_advertisement(
        db_session,
        category_id=profession.id,
        skill_ids=[skill.id],
    )

    results = search_public_job_advertisements(
        db_session,
        skill_ids=[skill.id],
    )

    assert results.total == 1
    assert results.items[0].id == advertisement.id
    assert any(result_skill.id == skill.id for result_skill in results.items[0].required_skills)


def test_filters_by_main_category_and_subcategory(
    db_session: Session,
) -> None:
    profession = create_category(
        db_session,
        f"Electrical {uuid4().hex[:8]}",
    )
    subcategory = create_category(
        db_session,
        f"Residential Wiring {uuid4().hex[:8]}",
        parent_id=profession.id,
    )

    advertisement, _, _ = create_published_advertisement(
        db_session,
        category_id=profession.id,
        subcategory_id=subcategory.id,
    )

    by_profession = search_public_job_advertisements(
        db_session,
        main_category_id=profession.id,
    )
    by_subcategory = search_public_job_advertisements(
        db_session,
        subcategory_id=subcategory.id,
    )

    assert by_profession.total == 1
    assert by_profession.items[0].id == advertisement.id

    assert by_subcategory.total == 1
    assert by_subcategory.items[0].id == advertisement.id


def test_publication_date_filters_include_the_selected_day(
    db_session: Session,
) -> None:
    older_ad, _, _ = create_published_advertisement(
        db_session,
        title="Older date filter advertisement",
    )
    current_ad, _, _ = create_published_advertisement(
        db_session,
        title="Current date filter advertisement",
    )

    today = datetime.now(UTC).date()

    older_ad.published_at = datetime.now(UTC) - timedelta(days=2)
    current_ad.published_at = datetime.now(UTC)
    db_session.flush()

    results = search_public_job_advertisements(
        db_session,
        published_after=today,
        published_before=today,
    )

    assert results.total == 1
    assert results.items[0].id == current_ad.id


def test_newest_oldest_and_relevance_sorting(
    db_session: Session,
) -> None:
    title_match, _, _ = create_published_advertisement(
        db_session,
        title="Electrician required",
        description="General maintenance work.",
    )
    description_match, _, _ = create_published_advertisement(
        db_session,
        title="Maintenance technician required",
        description="An experienced electrician is needed.",
    )

    now = datetime.now(UTC)
    title_match.published_at = now - timedelta(days=2)
    description_match.published_at = now
    db_session.flush()

    newest = search_public_job_advertisements(
        db_session,
        sort="newest",
    )
    oldest = search_public_job_advertisements(
        db_session,
        sort="oldest",
    )
    relevance = search_public_job_advertisements(
        db_session,
        keyword="electrician",
        sort="relevance",
    )

    assert newest.items[0].id == description_match.id
    assert oldest.items[0].id == title_match.id
    assert relevance.items[0].id == title_match.id


def test_whitespace_only_keyword_behaves_like_no_keyword(
    db_session: Session,
) -> None:
    advertisement, _, _ = create_published_advertisement(db_session)

    results = search_public_job_advertisements(
        db_session,
        keyword="   ",
    )

    assert results.total == 1
    assert results.items[0].id == advertisement.id
