from collections.abc import Generator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy.orm import Session

from app.database.session import engine
from app.models.employer import EmployerProfile
from app.models.identity import User
from app.models.job_advertisement import (
    EmployerPostingCreditEntry,
    JobAdvertisement,
    JobAdvertisementAudit,
    Payment,
)
from app.services.employers import create_employer_profile
from app.services.job_advertisements import (
    InsufficientPostingCreditsError,
    JobAdvertisementConflictError,
    JobAdvertisementNotFoundError,
    JobAdvertisementValidationError,
    close_job_advertisement,
    create_job_advertisement_draft,
    expire_due_job_advertisements,
    get_employer_job_advertisement,
    get_employer_posting_credit_balance,
    get_employer_posting_credit_summary,
    list_employer_job_advertisements,
    publish_job_advertisement,
    reinstate_job_advertisement,
    suspend_job_advertisement,
    update_job_advertisement,
    update_job_advertisement_positions_filled,
)
from app.services.locations import create_location
from app.services.taxonomy import (
    associate_skill,
    create_category,
    create_skill,
    set_category_active,
)


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


def create_test_employer(db: Session) -> tuple[User, EmployerProfile]:
    """Create an eligible user and an employer profile."""
    unique_id = uuid4().hex[:12]

    user = User(
        full_name="Job Advertisement Test User",
        username=f"jobad_{unique_id}",
        username_key=f"jobad_{unique_id}",
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
        employer_type="individual",
    )

    return user, employer


def grant_initial_posting_credits(
    db: Session,
    user: User,
    employer: EmployerProfile,
) -> Payment:
    """Create a verified sandbox payment and its initial five-credit grant."""
    payment = Payment(
        employer_profile_id=employer.id,
        initiated_by_user_id=user.id,
        purpose="initial_employer_package",
        provider="test",
        provider_environment="sandbox",
        idempotency_key=f"test-initial-package-{uuid4()}",
        amount=Decimal("50.00"),
        currency_code="KES",
        status="succeeded",
        verified_at=datetime.now(UTC),
    )
    db.add(payment)
    db.flush()

    grant = EmployerPostingCreditEntry(
        employer_profile_id=employer.id,
        entry_type="initial_grant",
        credit_delta=5,
        payment_id=payment.id,
    )
    db.add(grant)
    db.flush()

    return payment


def create_valid_draft(
    db: Session,
    user: User,
) -> JobAdvertisement:
    """Create a complete draft that satisfies publication requirements."""
    category = create_category(
        db,
        f"Construction {uuid4().hex[:8]}",
    )

    return create_job_advertisement_draft(
        db,
        user.id,
        {
            "title": "Roof Repair Specialist",
            "description": "Experienced roofer needed for residential roof repairs.",
            "job_type": "casual_daily",
            "work_arrangement": "remote",
            "main_category_id": category.id,
            "compensation_type": "negotiable",
            "workers_needed": 2,
            "contact_methods": ["in_app_message"],
        },
    )


def publish_test_advertisement(
    db: Session,
    user: User,
    employer: EmployerProfile,
) -> JobAdvertisement:
    """Create, fund, and publish a valid test advertisement."""
    grant_initial_posting_credits(db, user, employer)
    advertisement = create_valid_draft(db, user)

    return publish_job_advertisement(
        db,
        user.id,
        advertisement.id,
    )


def test_create_incomplete_draft_without_consuming_credit(
    db_session: Session,
) -> None:
    user, employer = create_test_employer(db_session)

    advertisement = create_job_advertisement_draft(
        db_session,
        user.id,
    )

    assert advertisement.id is not None
    assert advertisement.employer_profile_id == employer.id
    assert advertisement.status == "draft"
    assert advertisement.published_at is None
    assert advertisement.expires_at is None

    assert get_employer_posting_credit_balance(db_session, employer.id) == 0


def test_create_draft_normalizes_text_fields(
    db_session: Session,
) -> None:
    user, employer = create_test_employer(db_session)

    advertisement = create_job_advertisement_draft(
        db_session,
        user.id,
        {
            "title": "  Roof Repair Specialist  ",
            "description": "  Repair leaking roofs.  ",
        },
    )

    assert advertisement.title == "Roof Repair Specialist"
    assert advertisement.description == "Repair leaking roofs."
    assert advertisement.employer_profile_id == employer.id


def test_employer_cannot_update_another_employers_advertisement(
    db_session: Session,
) -> None:
    first_user, _ = create_test_employer(db_session)
    second_user, _ = create_test_employer(db_session)

    advertisement = create_job_advertisement_draft(
        db_session,
        first_user.id,
    )

    with pytest.raises(JobAdvertisementNotFoundError):
        update_job_advertisement(
            db_session,
            second_user.id,
            advertisement.id,
            {"title": "Unauthorized change"},
        )

    db_session.refresh(advertisement)
    assert advertisement.title is None
    assert advertisement.status == "draft"


def test_publish_advertisement_consumes_one_credit_and_assigns_expiry(
    db_session: Session,
) -> None:
    user, employer = create_test_employer(db_session)
    grant_initial_posting_credits(db_session, user, employer)
    advertisement = create_valid_draft(db_session, user)

    published = publish_job_advertisement(
        db_session,
        user.id,
        advertisement.id,
    )

    assert published.status == "published"
    assert published.published_at is not None
    assert published.expires_at is not None
    assert published.expires_at - published.published_at == timedelta(days=30)

    assert published.profession_snapshot is not None
    assert published.profession_snapshot["name"].startswith("Construction")
    assert published.skills_snapshot == []
    assert published.location_snapshot is None

    assert get_employer_posting_credit_balance(db_session, employer.id) == 4

    consumption_entries = (
        db_session.query(EmployerPostingCreditEntry)
        .filter_by(
            employer_profile_id=employer.id,
            entry_type="consumption",
            advertisement_id=advertisement.id,
        )
        .all()
    )
    assert len(consumption_entries) == 1
    assert consumption_entries[0].credit_delta == -1


def test_publish_without_credits_preserves_draft_and_ledger(
    db_session: Session,
) -> None:
    user, employer = create_test_employer(db_session)
    advertisement = create_valid_draft(db_session, user)

    with pytest.raises(InsufficientPostingCreditsError):
        publish_job_advertisement(
            db_session,
            user.id,
            advertisement.id,
        )

    db_session.refresh(advertisement)

    assert advertisement.status == "draft"
    assert advertisement.published_at is None
    assert advertisement.expires_at is None
    assert get_employer_posting_credit_balance(db_session, employer.id) == 0

    consumption_entries = (
        db_session.query(EmployerPostingCreditEntry)
        .filter_by(
            employer_profile_id=employer.id,
            entry_type="consumption",
        )
        .all()
    )
    assert consumption_entries == []


def test_published_advertisement_can_be_edited_without_consuming_credit(
    db_session: Session,
) -> None:
    user, employer = create_test_employer(db_session)
    advertisement = publish_test_advertisement(db_session, user, employer)

    original_published_at = advertisement.published_at
    original_expires_at = advertisement.expires_at

    updated = update_job_advertisement(
        db_session,
        user.id,
        advertisement.id,
        {
            "title": "Experienced Roof Repair Specialist",
            "description": "Roof repair work for residential properties.",
        },
    )

    assert updated.title == "Experienced Roof Repair Specialist"
    assert updated.description == "Roof repair work for residential properties."
    assert updated.status == "published"
    assert updated.published_at == original_published_at
    assert updated.expires_at == original_expires_at

    assert get_employer_posting_credit_balance(db_session, employer.id) == 4

    audit = (
        db_session.query(JobAdvertisementAudit)
        .filter_by(
            advertisement_id=advertisement.id,
            action="employer_edit",
        )
        .one()
    )

    assert audit.actor_user_id == user.id
    assert audit.changes["title"] == {
        "before": "Roof Repair Specialist",
        "after": "Experienced Roof Repair Specialist",
    }
    assert audit.changes["description"]["before"] == (
        "Experienced roofer needed for residential roof repairs."
    )


def test_updating_positions_filled_records_audit_and_preserves_credit(
    db_session: Session,
) -> None:
    user, employer = create_test_employer(db_session)
    advertisement = publish_test_advertisement(db_session, user, employer)

    updated = update_job_advertisement_positions_filled(
        db_session,
        user.id,
        advertisement.id,
        2,
    )

    assert updated.positions_filled == 2
    assert updated.workers_needed == 2
    assert updated.status == "published"

    audit = (
        db_session.query(JobAdvertisementAudit)
        .filter_by(
            advertisement_id=advertisement.id,
            action="employer_edit",
        )
        .one()
    )

    assert audit.actor_user_id == user.id
    assert audit.changes["positions_filled"] == {
        "before": 0,
        "after": 2,
    }

    assert get_employer_posting_credit_balance(db_session, employer.id) == 4


def test_positions_filled_can_be_decreased_again(
    db_session: Session,
) -> None:
    user, employer = create_test_employer(db_session)
    advertisement = publish_test_advertisement(db_session, user, employer)

    update_job_advertisement_positions_filled(
        db_session,
        user.id,
        advertisement.id,
        2,
    )

    updated = update_job_advertisement_positions_filled(
        db_session,
        user.id,
        advertisement.id,
        1,
    )

    assert updated.positions_filled == 1

    audits = (
        db_session.query(JobAdvertisementAudit)
        .filter_by(
            advertisement_id=advertisement.id,
            action="employer_edit",
        )
        .all()
    )

    assert len(audits) == 2
    assert audits[1].changes["positions_filled"] == {
        "before": 2,
        "after": 1,
    }

    assert get_employer_posting_credit_balance(db_session, employer.id) == 4


@pytest.mark.parametrize("invalid_value", [-1, True, 3])
def test_invalid_positions_filled_value_is_rejected(
    db_session: Session,
    invalid_value: int,
) -> None:
    user, employer = create_test_employer(db_session)
    advertisement = publish_test_advertisement(db_session, user, employer)

    with pytest.raises(JobAdvertisementValidationError):
        update_job_advertisement_positions_filled(
            db_session,
            user.id,
            advertisement.id,
            invalid_value,
        )

    db_session.refresh(advertisement)
    assert advertisement.positions_filled == 0
    assert get_employer_posting_credit_balance(db_session, employer.id) == 4


def test_close_advertisement_preserves_consumed_credit_and_records_audit(
    db_session: Session,
) -> None:
    user, employer = create_test_employer(db_session)
    advertisement = publish_test_advertisement(db_session, user, employer)

    closed = close_job_advertisement(
        db_session,
        user.id,
        advertisement.id,
    )

    assert closed.status == "closed"
    assert closed.closed_at is not None
    assert closed.published_at is not None
    assert closed.expires_at is not None

    assert get_employer_posting_credit_balance(db_session, employer.id) == 4

    consumption_entries = (
        db_session.query(EmployerPostingCreditEntry)
        .filter_by(
            employer_profile_id=employer.id,
            entry_type="consumption",
            advertisement_id=advertisement.id,
        )
        .all()
    )
    assert len(consumption_entries) == 1

    audit = (
        db_session.query(JobAdvertisementAudit)
        .filter_by(
            advertisement_id=advertisement.id,
            action="employer_close",
        )
        .one()
    )

    assert audit.actor_user_id == user.id
    assert audit.changes["closed_at"] == closed.closed_at.isoformat()


def test_employer_can_list_only_their_own_advertisements(
    db_session: Session,
) -> None:
    first_user, first_employer = create_test_employer(db_session)
    second_user, _ = create_test_employer(db_session)

    first_ad = create_job_advertisement_draft(db_session, first_user.id)
    second_ad = create_job_advertisement_draft(db_session, second_user.id)

    advertisements, total = list_employer_job_advertisements(
        db_session,
        first_user.id,
    )

    assert total == 1
    assert len(advertisements) == 1
    assert advertisements[0].id == first_ad.id
    assert advertisements[0].employer_profile_id == first_employer.id
    assert advertisements[0].id != second_ad.id


def test_employer_advertisement_list_supports_status_filter(
    db_session: Session,
) -> None:
    user, employer = create_test_employer(db_session)

    draft = create_job_advertisement_draft(db_session, user.id)
    published = publish_test_advertisement(db_session, user, employer)

    drafts, draft_total = list_employer_job_advertisements(
        db_session,
        user.id,
        status_filter="draft",
    )
    published_ads, published_total = list_employer_job_advertisements(
        db_session,
        user.id,
        status_filter="published",
    )

    assert draft_total == 1
    assert [advertisement.id for advertisement in drafts] == [draft.id]

    assert published_total == 1
    assert [advertisement.id for advertisement in published_ads] == [published.id]


def test_employer_can_retrieve_their_own_advertisement(
    db_session: Session,
) -> None:
    user, _ = create_test_employer(db_session)
    advertisement = create_job_advertisement_draft(db_session, user.id)

    retrieved = get_employer_job_advertisement(
        db_session,
        user.id,
        advertisement.id,
    )

    assert retrieved.id == advertisement.id
    assert retrieved.employer_profile_id == advertisement.employer_profile_id


def test_employer_cannot_retrieve_another_employers_advertisement(
    db_session: Session,
) -> None:
    first_user, _ = create_test_employer(db_session)
    second_user, _ = create_test_employer(db_session)

    advertisement = create_job_advertisement_draft(
        db_session,
        first_user.id,
    )

    with pytest.raises(JobAdvertisementNotFoundError):
        get_employer_job_advertisement(
            db_session,
            second_user.id,
            advertisement.id,
        )


def test_employer_posting_credit_summary(
    db_session: Session,
) -> None:
    user, employer = create_test_employer(db_session)
    grant_initial_posting_credits(db_session, user, employer)

    summary = get_employer_posting_credit_summary(
        db_session,
        user.id,
    )

    assert summary["available_credits"] == 5
    assert summary["initial_package_credits"] == 5
    assert summary["initial_package_price"] == Decimal("50.00")
    assert summary["additional_credit_price"] == Decimal("50.00")
    assert summary["currency_code"] == "KES"


def test_employer_advertisement_list_supports_pagination(
    db_session: Session,
) -> None:
    user, _ = create_test_employer(db_session)

    created = [create_job_advertisement_draft(db_session, user.id) for _ in range(3)]

    first_page, total = list_employer_job_advertisements(
        db_session,
        user.id,
        page=1,
        page_size=2,
    )
    second_page, second_total = list_employer_job_advertisements(
        db_session,
        user.id,
        page=2,
        page_size=2,
    )

    assert total == 3
    assert second_total == 3
    assert len(first_page) == 2
    assert len(second_page) == 1

    returned_ids = {advertisement.id for advertisement in first_page + second_page}
    assert returned_ids == {advertisement.id for advertisement in created}


def create_draft_with_arrangement(
    db: Session,
    user: User,
    work_arrangement: str,
    location_id=None,
) -> JobAdvertisement:
    """Create a complete draft with a chosen work arrangement and location."""
    category = create_category(
        db,
        f"Construction {uuid4().hex[:8]}",
    )

    values = {
        "title": "Roof Repair Specialist",
        "description": "Experienced roofer needed for residential roof repairs.",
        "job_type": "casual_daily",
        "work_arrangement": work_arrangement,
        "main_category_id": category.id,
        "compensation_type": "negotiable",
        "workers_needed": 2,
        "contact_methods": ["in_app_message"],
    }
    if location_id is not None:
        values["location_id"] = location_id

    return create_job_advertisement_draft(db, user.id, values)


def create_test_town(db: Session):
    """Create a country, county, and town hierarchy and return the town."""
    country = create_location(
        db,
        "country",
        "Kenya",
        country_code="KE",
    )
    county = create_location(
        db,
        "county",
        f"Test County {uuid4().hex[:8]}",
        parent_id=country.id,
    )
    return create_location(
        db,
        "town",
        f"Test Town {uuid4().hex[:8]}",
        parent_id=county.id,
    )


@pytest.mark.parametrize("work_arrangement", ["on_site", "hybrid"])
def test_publish_requires_location_for_on_site_and_hybrid(
    db_session: Session,
    work_arrangement: str,
) -> None:
    user, employer = create_test_employer(db_session)
    grant_initial_posting_credits(db_session, user, employer)
    advertisement = create_draft_with_arrangement(
        db_session,
        user,
        work_arrangement,
    )

    with pytest.raises(
        JobAdvertisementValidationError,
        match="work location is required",
    ):
        publish_job_advertisement(
            db_session,
            user.id,
            advertisement.id,
        )

    db_session.refresh(advertisement)

    assert advertisement.status == "draft"
    assert advertisement.published_at is None
    assert advertisement.expires_at is None
    assert get_employer_posting_credit_balance(db_session, employer.id) == 5

    consumption_entries = (
        db_session.query(EmployerPostingCreditEntry)
        .filter_by(
            employer_profile_id=employer.id,
            entry_type="consumption",
            advertisement_id=advertisement.id,
        )
        .all()
    )
    assert consumption_entries == []


@pytest.mark.parametrize("work_arrangement", ["on_site", "hybrid"])
def test_publish_on_site_and_hybrid_with_location_succeeds(
    db_session: Session,
    work_arrangement: str,
) -> None:
    user, employer = create_test_employer(db_session)
    grant_initial_posting_credits(db_session, user, employer)
    town = create_test_town(db_session)
    advertisement = create_draft_with_arrangement(
        db_session,
        user,
        work_arrangement,
        location_id=town.id,
    )

    published = publish_job_advertisement(
        db_session,
        user.id,
        advertisement.id,
    )

    assert published.status == "published"
    assert published.work_arrangement == work_arrangement
    assert published.location_id == town.id
    assert published.location_snapshot is not None
    assert get_employer_posting_credit_balance(db_session, employer.id) == 4


def test_publish_remote_without_location_succeeds(
    db_session: Session,
) -> None:
    user, employer = create_test_employer(db_session)
    grant_initial_posting_credits(db_session, user, employer)
    advertisement = create_draft_with_arrangement(
        db_session,
        user,
        "remote",
    )

    published = publish_job_advertisement(
        db_session,
        user.id,
        advertisement.id,
    )

    assert published.status == "published"
    assert published.location_id is None
    assert published.location_snapshot is None
    assert get_employer_posting_credit_balance(db_session, employer.id) == 4


def _expiry_cutoff(advertisement: JobAdvertisement) -> datetime:
    return advertisement.expires_at + timedelta(seconds=1)


def test_expire_due_advertisement_transitions_status_and_audits(
    db_session: Session,
) -> None:
    user, employer = create_test_employer(db_session)
    advertisement = publish_test_advertisement(db_session, user, employer)
    original_expiry = advertisement.expires_at

    expired = expire_due_job_advertisements(
        db_session,
        now=_expiry_cutoff(advertisement),
    )

    assert expired == 1
    db_session.refresh(advertisement)
    assert advertisement.status == "expired"
    assert advertisement.expires_at == original_expiry
    assert get_employer_posting_credit_balance(db_session, employer.id) == 4

    audits = (
        db_session.query(JobAdvertisementAudit)
        .filter_by(advertisement_id=advertisement.id, action="expire")
        .all()
    )
    assert len(audits) == 1
    assert audits[0].actor_user_id is None
    assert audits[0].changes["status"] == {"before": "published", "after": "expired"}


def test_expire_leaves_not_yet_due_advertisement_published(
    db_session: Session,
) -> None:
    user, employer = create_test_employer(db_session)
    advertisement = publish_test_advertisement(db_session, user, employer)

    expired = expire_due_job_advertisements(
        db_session,
        now=advertisement.expires_at - timedelta(seconds=1),
    )

    assert expired == 0
    db_session.refresh(advertisement)
    assert advertisement.status == "published"


def test_expire_ignores_draft_closed_and_suspended_advertisements(
    db_session: Session,
) -> None:
    user, employer = create_test_employer(db_session)
    grant_initial_posting_credits(db_session, user, employer)

    draft = create_valid_draft(db_session, user)

    closed = publish_job_advertisement(db_session, user.id, create_valid_draft(db_session, user).id)
    close_job_advertisement(db_session, user.id, closed.id)

    suspended = publish_job_advertisement(
        db_session, user.id, create_valid_draft(db_session, user).id
    )
    suspend_job_advertisement(
        db_session,
        user.id,
        suspended.id,
        "Testing moderation status preservation.",
    )

    expired = expire_due_job_advertisements(
        db_session,
        now=datetime.now(UTC) + timedelta(days=365),
    )

    assert expired == 0
    for advertisement, status in (
        (draft, "draft"),
        (closed, "closed"),
        (suspended, "suspended"),
    ):
        db_session.refresh(advertisement)
        assert advertisement.status == status


def test_expire_is_idempotent(db_session: Session) -> None:
    user, employer = create_test_employer(db_session)
    advertisement = publish_test_advertisement(db_session, user, employer)
    cutoff = _expiry_cutoff(advertisement)

    assert expire_due_job_advertisements(db_session, now=cutoff) == 1
    assert expire_due_job_advertisements(db_session, now=cutoff) == 0

    audits = (
        db_session.query(JobAdvertisementAudit)
        .filter_by(advertisement_id=advertisement.id, action="expire")
        .count()
    )
    assert audits == 1


def test_expire_respects_batch_size(db_session: Session) -> None:
    user, employer = create_test_employer(db_session)
    grant_initial_posting_credits(db_session, user, employer)

    published = [
        publish_job_advertisement(db_session, user.id, create_valid_draft(db_session, user).id)
        for _ in range(3)
    ]
    cutoff = max(ad.expires_at for ad in published) + timedelta(seconds=1)

    assert expire_due_job_advertisements(db_session, batch_size=2, now=cutoff) == 2
    assert expire_due_job_advertisements(db_session, batch_size=2, now=cutoff) == 1
    assert expire_due_job_advertisements(db_session, batch_size=2, now=cutoff) == 0


@pytest.mark.parametrize("batch_size", [0, -1, True])
def test_expire_rejects_invalid_batch_size(
    db_session: Session,
    batch_size: int,
) -> None:
    with pytest.raises(JobAdvertisementValidationError):
        expire_due_job_advertisements(db_session, batch_size=batch_size)


def _lapse(db: Session, advertisement: JobAdvertisement) -> None:
    """Move a published advertisement's expiry into the past without running the expiry job."""
    advertisement.published_at = datetime.now(UTC) - timedelta(days=31)
    advertisement.expires_at = datetime.now(UTC) - timedelta(days=1)
    db.flush()


def test_employer_cannot_edit_lapsed_advertisement(db_session: Session) -> None:
    user, employer = create_test_employer(db_session)
    advertisement = publish_test_advertisement(db_session, user, employer)
    _lapse(db_session, advertisement)

    with pytest.raises(JobAdvertisementConflictError, match="expired"):
        update_job_advertisement(db_session, user.id, advertisement.id, {"title": "Late edit"})

    db_session.refresh(advertisement)
    assert advertisement.title == "Roof Repair Specialist"
    assert advertisement.status == "published"


def test_employer_cannot_close_lapsed_advertisement(db_session: Session) -> None:
    user, employer = create_test_employer(db_session)
    advertisement = publish_test_advertisement(db_session, user, employer)
    _lapse(db_session, advertisement)

    with pytest.raises(JobAdvertisementConflictError, match="expired"):
        close_job_advertisement(db_session, user.id, advertisement.id)

    db_session.refresh(advertisement)
    assert advertisement.status == "published"
    assert advertisement.closed_at is None


def test_employer_cannot_update_positions_on_lapsed_advertisement(db_session: Session) -> None:
    user, employer = create_test_employer(db_session)
    advertisement = publish_test_advertisement(db_session, user, employer)
    _lapse(db_session, advertisement)

    with pytest.raises(JobAdvertisementConflictError, match="expired"):
        update_job_advertisement_positions_filled(db_session, user.id, advertisement.id, 1)

    db_session.refresh(advertisement)
    assert advertisement.positions_filled == 0


def test_lapsed_advertisement_suspension_stays_suspended_and_cannot_reinstate(
    db_session: Session,
) -> None:
    user, employer = create_test_employer(db_session)
    advertisement = publish_test_advertisement(db_session, user, employer)
    _lapse(db_session, advertisement)

    suspend_job_advertisement(db_session, user.id, advertisement.id, "Moderation review.")
    assert advertisement.status == "suspended"

    with pytest.raises(JobAdvertisementConflictError, match="expired"):
        reinstate_job_advertisement(db_session, user.id, advertisement.id, "Cleared.")

    assert expire_due_job_advertisements(db_session) == 0
    db_session.refresh(advertisement)
    assert advertisement.status == "suspended"


def _skill_draft(db: Session, user: User, profession, skill) -> JobAdvertisement:
    return create_job_advertisement_draft(
        db,
        user.id,
        {
            "title": "Roof Repair Specialist",
            "description": "Experienced roofer needed for residential roof repairs.",
            "job_type": "casual_daily",
            "work_arrangement": "remote",
            "main_category_id": profession.id,
            "compensation_type": "negotiable",
            "workers_needed": 1,
            "contact_methods": ["in_app_message"],
            "skill_ids": [skill.id],
        },
    )


def test_publish_accepts_skill_under_active_descendant_category(db_session: Session) -> None:
    user, employer = create_test_employer(db_session)
    grant_initial_posting_credits(db_session, user, employer)
    profession = create_category(db_session, f"Construction {uuid4().hex[:8]}")
    child = create_category(db_session, f"Roofing {uuid4().hex[:8]}", parent_id=profession.id)
    skill = create_skill(db_session, f"Metal roofing {uuid4().hex[:8]}")
    associate_skill(db_session, child.id, skill.id)
    draft = _skill_draft(db_session, user, profession, skill)

    published = publish_job_advertisement(db_session, user.id, draft.id)

    assert [entry["id"] for entry in published.skills_snapshot] == [str(skill.id)]


def test_publish_rejects_skill_under_deactivated_descendant_category(db_session: Session) -> None:
    user, employer = create_test_employer(db_session)
    grant_initial_posting_credits(db_session, user, employer)
    profession = create_category(db_session, f"Construction {uuid4().hex[:8]}")
    child = create_category(db_session, f"Roofing {uuid4().hex[:8]}", parent_id=profession.id)
    skill = create_skill(db_session, f"Metal roofing {uuid4().hex[:8]}")
    associate_skill(db_session, child.id, skill.id)
    draft = _skill_draft(db_session, user, profession, skill)
    set_category_active(db_session, child.id, False)

    with pytest.raises(JobAdvertisementValidationError, match="belong to the selected profession"):
        publish_job_advertisement(db_session, user.id, draft.id)

    db_session.refresh(draft)
    assert draft.status == "draft"
    assert get_employer_posting_credit_balance(db_session, employer.id) == 5


def test_publish_rejects_skill_under_active_grandchild_of_deactivated_branch(
    db_session: Session,
) -> None:
    user, employer = create_test_employer(db_session)
    grant_initial_posting_credits(db_session, user, employer)
    profession = create_category(db_session, f"Construction {uuid4().hex[:8]}")
    middle = create_category(db_session, f"Roofing {uuid4().hex[:8]}", parent_id=profession.id)
    leaf = create_category(db_session, f"Repairs {uuid4().hex[:8]}", parent_id=middle.id)
    skill = create_skill(db_session, f"Leak repair {uuid4().hex[:8]}")
    associate_skill(db_session, leaf.id, skill.id)
    draft = _skill_draft(db_session, user, profession, skill)
    set_category_active(db_session, middle.id, False)

    with pytest.raises(JobAdvertisementValidationError, match="belong to the selected profession"):
        publish_job_advertisement(db_session, user.id, draft.id)

    assert get_employer_posting_credit_balance(db_session, employer.id) == 5
