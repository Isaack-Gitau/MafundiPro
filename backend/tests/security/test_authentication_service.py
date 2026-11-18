from collections.abc import Generator
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.security import hash_password, verify_password
from app.models.identity import User
from app.services.authentication import authenticate_user


@pytest.fixture(scope="session")
def authentication_test_engine():
    """Connect exclusively to the isolated PostgreSQL test database."""
    test_url = settings.database_url.set(database="mafundipro_test")
    engine = create_engine(test_url, pool_pre_ping=True)

    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture
def db_session(authentication_test_engine) -> Generator[Session]:
    """Provide an isolated transaction for each test."""
    with authentication_test_engine.connect() as connection:
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


@pytest.fixture(scope="session")
def valid_password_hash() -> str:
    """Provide a genuine Argon2id password hash for authentication tests."""
    return hash_password("Correct-Horse-Battery-2026!")


def create_user(
    db: Session,
    password_hash: str,
    *,
    username: str | None = None,
    account_status: str = "active",
    verified: bool = True,
) -> User:
    """Create a user with configurable authentication eligibility."""
    marker = uuid4().hex[:12]
    normalized_username = username or f"auth_{marker}"

    user = User(
        full_name="Authentication Test User",
        username=normalized_username,
        username_key=normalized_username.casefold(),
        phone_number=f"+2547{uuid4().int % 100_000_000:08d}",
        password_hash=password_hash,
        account_status=account_status,
        phone_verified_at=datetime.now(UTC) if verified else None,
    )
    db.add(user)
    db.flush()

    return user


def test_active_verified_user_can_authenticate(
    db_session: Session,
    valid_password_hash: str,
) -> None:
    user = create_user(db_session, valid_password_hash)

    authenticated = authenticate_user(
        db_session,
        user.username,
        "Correct-Horse-Battery-2026!",
    )

    assert authenticated is not None
    assert authenticated.id == user.id


def test_username_lookup_is_case_insensitive(
    db_session: Session,
    valid_password_hash: str,
) -> None:
    user = create_user(
        db_session,
        valid_password_hash,
        username="MafundiWorker",
    )

    authenticated = authenticate_user(
        db_session,
        "MAFUNDIWORKER",
        "Correct-Horse-Battery-2026!",
    )

    assert authenticated is not None
    assert authenticated.id == user.id


def test_incorrect_password_is_rejected(
    db_session: Session,
    valid_password_hash: str,
) -> None:
    user = create_user(db_session, valid_password_hash)

    authenticated = authenticate_user(
        db_session,
        user.username,
        "Wrong-Password-2026!",
    )

    assert authenticated is None


def test_unknown_username_is_rejected(db_session: Session) -> None:
    authenticated = authenticate_user(
        db_session,
        "unknown_worker",
        "Correct-Horse-Battery-2026!",
    )

    assert authenticated is None


@pytest.mark.parametrize(
    ("account_status", "verified"),
    [
        ("pending_verification", True),
        ("suspended", True),
        ("deactivated", True),
        ("active", False),
    ],
)
def test_ineligible_accounts_cannot_authenticate(
    db_session: Session,
    valid_password_hash: str,
    account_status: str,
    verified: bool,
) -> None:
    user = create_user(
        db_session,
        valid_password_hash,
        account_status=account_status,
        verified=verified,
    )

    authenticated = authenticate_user(
        db_session,
        user.username,
        "Correct-Horse-Battery-2026!",
    )

    assert authenticated is None


def test_malformed_stored_password_hash_is_rejected(
    db_session: Session,
) -> None:
    user = create_user(db_session, "not-a-valid-argon2-hash")

    authenticated = authenticate_user(
        db_session,
        user.username,
        "Correct-Horse-Battery-2026!",
    )

    assert authenticated is None


def test_successful_authentication_rehashes_outdated_password(
    db_session: Session,
    valid_password_hash: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = create_user(db_session, valid_password_hash)
    original_hash = user.password_hash

    monkeypatch.setattr(
        "app.services.authentication.password_needs_rehash",
        lambda password_hash: True,
    )

    authenticated = authenticate_user(
        db_session,
        user.username,
        "Correct-Horse-Battery-2026!",
    )

    assert authenticated is not None
    assert user.password_hash != original_hash
    assert verify_password(
        "Correct-Horse-Battery-2026!",
        user.password_hash,
    )
