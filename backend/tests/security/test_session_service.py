from collections.abc import Generator
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, update
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.security import hash_session_token
from app.models.identity import User
from app.services.sessions import (
    SessionNotEligibleError,
    create_session,
    get_valid_session,
    revoke_session,
)


@pytest.fixture(scope="session")
def session_test_engine():
    """Connect exclusively to the isolated PostgreSQL test database."""
    test_url = settings.database_url.set(database="mafundipro_test")
    engine = create_engine(test_url, pool_pre_ping=True)

    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture
def db_session(session_test_engine) -> Generator[Session]:
    """Provide an isolated transaction for each test."""
    with session_test_engine.connect() as connection:
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
            if transaction.is_active:
                transaction.rollback()


def create_active_user(
    db: Session,
    *,
    account_status: str = "active",
    verified: bool = True,
) -> User:
    """Create an account for session lifecycle tests."""
    marker = uuid4().hex[:12]
    user = User(
        full_name="Session Test User",
        username=f"session_{marker}",
        username_key=f"session_{marker}",
        phone_number=f"+2547{uuid4().int % 100_000_000:08d}",
        password_hash="test-password-hash",
        account_status=account_status,
        phone_verified_at=datetime.now(UTC) if verified else None,
    )
    db.add(user)
    db.flush()
    return user


def test_create_session_for_active_verified_user(db_session: Session) -> None:
    now = datetime(2026, 11, 18, 12, 0, tzinfo=UTC)
    user = create_active_user(db_session)

    token, user_session = create_session(db_session, user.id, now=now)

    assert token
    assert user_session.user_id == user.id
    assert user_session.token_hash == hash_session_token(token)
    assert user_session.token_hash != token
    assert user_session.expires_at == now + timedelta(minutes=30)
    assert user_session.absolute_expires_at == now + timedelta(days=7)


@pytest.mark.parametrize(
    ("account_status", "verified"),
    [
        ("pending_verification", True),
        ("suspended", True),
        ("deactivated", True),
        ("active", False),
    ],
)
def test_ineligible_accounts_cannot_create_sessions(
    db_session: Session,
    account_status: str,
    verified: bool,
) -> None:
    user = create_active_user(
        db_session,
        account_status=account_status,
        verified=verified,
    )

    with pytest.raises(SessionNotEligibleError):
        create_session(db_session, user.id)


def test_valid_session_refreshes_idle_expiry(db_session: Session) -> None:
    now = datetime(2026, 11, 18, 12, 0, tzinfo=UTC)
    user = create_active_user(db_session)
    token, original = create_session(db_session, user.id, now=now)

    later = now + timedelta(minutes=10)
    validated = get_valid_session(db_session, token, now=later)

    assert validated is not None
    assert validated.id == original.id
    assert validated.last_seen_at == later
    assert validated.expires_at == later + timedelta(minutes=30)


def test_session_activity_is_not_refreshed_within_interval(
    db_session: Session,
) -> None:
    now = datetime(2026, 11, 18, 12, 0, tzinfo=UTC)
    user = create_active_user(db_session)
    token, user_session = create_session(db_session, user.id, now=now)

    original_last_seen = user_session.last_seen_at
    original_expiry = user_session.expires_at

    validated = get_valid_session(
        db_session,
        token,
        now=now + timedelta(seconds=30),
    )

    assert validated is not None
    assert validated.last_seen_at == original_last_seen
    assert validated.expires_at == original_expiry


def test_idle_expired_session_is_rejected_and_revoked(
    db_session: Session,
) -> None:
    now = datetime(2026, 11, 18, 12, 0, tzinfo=UTC)
    user = create_active_user(db_session)
    token, user_session = create_session(db_session, user.id, now=now)

    result = get_valid_session(
        db_session,
        token,
        now=now + timedelta(minutes=30),
    )

    assert result is None
    assert user_session.revoked_at == now + timedelta(minutes=30)


def test_absolute_expiry_cannot_be_extended(db_session: Session) -> None:
    now = datetime(2026, 11, 18, 12, 0, tzinfo=UTC)
    user = create_active_user(db_session)
    token, user_session = create_session(db_session, user.id, now=now)

    absolute_expiry = user_session.absolute_expires_at

    user_session.expires_at = absolute_expiry
    db_session.flush()

    before_absolute_expiry = absolute_expiry - timedelta(minutes=10)
    validated = get_valid_session(
        db_session,
        token,
        now=before_absolute_expiry,
    )

    assert validated is not None
    assert validated.expires_at == absolute_expiry

    result = get_valid_session(
        db_session,
        token,
        now=absolute_expiry,
    )

    assert result is None
    assert user_session.revoked_at == absolute_expiry


def test_revoked_session_cannot_be_reused(db_session: Session) -> None:
    now = datetime(2026, 11, 18, 12, 0, tzinfo=UTC)
    user = create_active_user(db_session)
    token, user_session = create_session(db_session, user.id, now=now)

    assert revoke_session(db_session, token, now=now) is True
    assert (
        get_valid_session(
            db_session,
            token,
            now=now + timedelta(minutes=1),
        )
        is None
    )
    assert user_session.revoked_at == now


def test_repeated_session_revocation_is_harmless(
    db_session: Session,
) -> None:
    now = datetime(2026, 11, 18, 12, 0, tzinfo=UTC)
    user = create_active_user(db_session)
    token, _ = create_session(db_session, user.id, now=now)

    assert revoke_session(db_session, token, now=now) is True
    assert (
        revoke_session(
            db_session,
            token,
            now=now + timedelta(seconds=1),
        )
        is False
    )


def test_suspended_account_session_is_revoked(db_session: Session) -> None:
    now = datetime(2026, 11, 18, 12, 0, tzinfo=UTC)
    user = create_active_user(db_session)
    token, user_session = create_session(db_session, user.id, now=now)

    user.account_status = "suspended"
    db_session.flush()

    result = get_valid_session(
        db_session,
        token,
        now=now + timedelta(minutes=1),
    )

    assert result is None
    assert user_session.revoked_at == now + timedelta(minutes=1)


def test_stale_user_identity_is_refreshed_before_session_validation(
    db_session: Session,
) -> None:
    now = datetime(2026, 11, 18, 12, 0, tzinfo=UTC)
    user = create_active_user(db_session)
    token, user_session = create_session(db_session, user.id, now=now)

    # Simulate a database update that bypasses SQLAlchemy's identity map.
    db_session.execute(
        update(User)
        .where(User.id == user.id)
        .values(account_status="suspended")
        .execution_options(synchronize_session=False)
    )

    # The in-memory object deliberately remains stale.
    assert user.account_status == "active"

    result = get_valid_session(
        db_session,
        token,
        now=now + timedelta(minutes=2),
    )

    assert result is None
    assert user.account_status == "suspended"
    assert user_session.revoked_at == now + timedelta(minutes=2)


def test_session_rejects_naive_timestamps(db_session: Session) -> None:
    user = create_active_user(db_session)
    naive_time = datetime(2026, 11, 18, 12, 0)

    with pytest.raises(ValueError, match="timezone-aware"):
        create_session(db_session, user.id, now=naive_time)
