from collections.abc import Generator
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.identity import PhoneVerification, User, UserRole, UserSession


@pytest.fixture(scope="session")
def test_engine():
    """Connect exclusively to the isolated PostgreSQL test database."""
    test_url = settings.database_url.set(database="mafundipro_test")

    assert test_url.database == "mafundipro_test"

    engine = create_engine(test_url, pool_pre_ping=True)

    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture
def db_session(test_engine) -> Generator[Session]:
    """Provide a transaction that is rolled back after each test."""
    with test_engine.connect() as connection:
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


def create_test_user(db_session: Session, **overrides) -> User:
    """Create a user with unique test identity values."""
    marker = uuid4().hex[:12]

    values = {
        "full_name": "Identity Test User",
        "username": f"user_{marker}",
        "username_key": f"user_{marker}",
        "phone_number": f"+2547{uuid4().int % 100_000_000:08d}",
        "password_hash": "test-argon2id-hash",
    }
    values.update(overrides)

    user = User(**values)
    db_session.add(user)
    db_session.flush()

    return user


def test_user_defaults_to_pending_verification(db_session: Session) -> None:
    user = create_test_user(db_session)

    assert user.account_status == "pending_verification"
    assert user.phone_verified_at is None


def test_user_rejects_duplicate_username_key(db_session: Session) -> None:
    marker = uuid4().hex[:12]

    create_test_user(
        db_session,
        username=f"user_{marker}",
        username_key=f"user_{marker}",
    )

    duplicate = User(
        full_name="Duplicate User",
        username=f"different_{marker}",
        username_key=f"user_{marker}",
        phone_number=f"+2547{uuid4().int % 100_000_000:08d}",
        password_hash="test-argon2id-hash",
    )

    db_session.add(duplicate)

    with pytest.raises(IntegrityError):
        db_session.flush()


def test_user_rejects_duplicate_phone_number(db_session: Session) -> None:
    phone_number = f"+2547{uuid4().int % 100_000_000:08d}"

    create_test_user(db_session, phone_number=phone_number)

    duplicate = User(
        full_name="Duplicate User",
        username=f"user_{uuid4().hex[:12]}",
        username_key=f"user_{uuid4().hex[:12]}",
        phone_number=phone_number,
        password_hash="test-argon2id-hash",
    )

    db_session.add(duplicate)

    with pytest.raises(IntegrityError):
        db_session.flush()


def test_user_rejects_invalid_account_status(db_session: Session) -> None:
    user = create_test_user(db_session)
    user.account_status = "unknown_status"

    with pytest.raises(IntegrityError):
        db_session.flush()


@pytest.mark.parametrize("role", ["admin", "moderator"])
def test_user_accepts_valid_platform_roles(
    db_session: Session,
    role: str,
) -> None:
    user = create_test_user(db_session)

    user_role = UserRole(user_id=user.id, role=role)
    db_session.add(user_role)
    db_session.flush()

    assert user_role.role == role


def test_user_rejects_worker_as_platform_role(db_session: Session) -> None:
    user = create_test_user(db_session)

    db_session.add(UserRole(user_id=user.id, role="worker"))

    with pytest.raises(IntegrityError):
        db_session.flush()


def test_user_rejects_duplicate_platform_role(db_session: Session) -> None:
    user = create_test_user(db_session)

    db_session.add(UserRole(user_id=user.id, role="admin"))
    db_session.flush()

    db_session.add(UserRole(user_id=user.id, role="admin"))

    with pytest.raises(IntegrityError):
        db_session.flush()


def test_session_accepts_valid_expiry_order(db_session: Session) -> None:
    user = create_test_user(db_session)
    now = datetime.now(UTC)

    session = UserSession(
        user_id=user.id,
        token_hash=uuid4().hex + uuid4().hex,
        expires_at=now + timedelta(minutes=30),
        absolute_expires_at=now + timedelta(days=7),
    )

    db_session.add(session)
    db_session.flush()

    assert session.expires_at < session.absolute_expires_at


def test_session_rejects_expiry_after_absolute_expiry(
    db_session: Session,
) -> None:
    user = create_test_user(db_session)
    now = datetime.now(UTC)

    session = UserSession(
        user_id=user.id,
        token_hash=uuid4().hex + uuid4().hex,
        expires_at=now + timedelta(days=8),
        absolute_expires_at=now + timedelta(days=7),
    )

    db_session.add(session)

    with pytest.raises(IntegrityError):
        db_session.flush()


def test_session_rejects_duplicate_token_hash(db_session: Session) -> None:
    user = create_test_user(db_session)
    now = datetime.now(UTC)
    token_hash = uuid4().hex + uuid4().hex

    first = UserSession(
        user_id=user.id,
        token_hash=token_hash,
        expires_at=now + timedelta(minutes=30),
        absolute_expires_at=now + timedelta(days=7),
    )

    db_session.add(first)
    db_session.flush()

    second = UserSession(
        user_id=user.id,
        token_hash=token_hash,
        expires_at=now + timedelta(minutes=30),
        absolute_expires_at=now + timedelta(days=7),
    )

    db_session.add(second)

    with pytest.raises(IntegrityError):
        db_session.flush()


@pytest.mark.parametrize("purpose", ["registration", "phone_change"])
def test_phone_verification_accepts_valid_purposes(
    db_session: Session,
    purpose: str,
) -> None:
    user = create_test_user(db_session)

    verification = PhoneVerification(
        user_id=user.id,
        phone_number=user.phone_number,
        code_hash=uuid4().hex + uuid4().hex,
        purpose=purpose,
        expires_at=datetime.now(UTC) + timedelta(minutes=10),
    )

    db_session.add(verification)
    db_session.flush()

    assert verification.purpose == purpose


def test_phone_verification_rejects_invalid_purpose(
    db_session: Session,
) -> None:
    user = create_test_user(db_session)

    verification = PhoneVerification(
        user_id=user.id,
        phone_number=user.phone_number,
        code_hash=uuid4().hex + uuid4().hex,
        purpose="password_reset",
        expires_at=datetime.now(UTC) + timedelta(minutes=10),
    )

    db_session.add(verification)

    with pytest.raises(IntegrityError):
        db_session.flush()


def test_phone_verification_rejects_negative_attempts(
    db_session: Session,
) -> None:
    user = create_test_user(db_session)

    verification = PhoneVerification(
        user_id=user.id,
        phone_number=user.phone_number,
        code_hash=uuid4().hex + uuid4().hex,
        purpose="registration",
        expires_at=datetime.now(UTC) + timedelta(minutes=10),
        attempts=-1,
    )

    db_session.add(verification)

    with pytest.raises(IntegrityError):
        db_session.flush()
