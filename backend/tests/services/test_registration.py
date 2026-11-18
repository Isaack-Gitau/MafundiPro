from collections.abc import Generator

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.security import verify_password
from app.models.identity import User
from app.schemas.auth import RegistrationCreate
from app.services.registration import (
    RegistrationConflictError,
    register_user,
)


@pytest.fixture
def test_engine():
    engine = create_engine(
        settings.database_url.set(database="mafundipro_test"),
        pool_pre_ping=True,
    )
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture
def db_session(test_engine) -> Generator[Session]:
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
            transaction.rollback()


def make_registration(**overrides) -> RegistrationCreate:
    values = {
        "full_name": "Test User",
        "username": "test_user",
        "phone_number": "0712345678",
        "password": "a-secure-password-123",
    }
    values.update(overrides)
    return RegistrationCreate(**values)


def test_register_user_creates_pending_account(db_session: Session) -> None:
    registration = make_registration()

    user = register_user(db_session, registration)

    assert user.id is not None
    assert user.full_name == "Test User"
    assert user.username == "test_user"
    assert user.username_key == "test_user"
    assert user.phone_number == "+254712345678"
    assert user.account_status == "pending_verification"
    assert user.phone_verified_at is None


def test_register_user_hashes_password(db_session: Session) -> None:
    registration = make_registration(password="a-secure-password-123")

    user = register_user(db_session, registration)

    assert user.password_hash != registration.password
    assert verify_password(registration.password, user.password_hash)


def test_register_user_does_not_create_roles_or_sessions(
    db_session: Session,
) -> None:
    user = register_user(db_session, make_registration())

    assert user.roles == []
    assert user.sessions == []
    assert user.phone_verifications == []


def test_register_user_normalizes_username_key(
    db_session: Session,
) -> None:
    registration = make_registration(username="Test.User")

    user = register_user(db_session, registration)

    assert user.username == "Test.User"
    assert user.username_key == "test.user"


def test_register_user_rejects_duplicate_username(
    db_session: Session,
) -> None:
    register_user(db_session, make_registration())

    duplicate = make_registration(
        username="TEST_USER",
        phone_number="0112345678",
    )

    with pytest.raises(RegistrationConflictError):
        register_user(db_session, duplicate)


def test_register_user_rejects_duplicate_phone_number(
    db_session: Session,
) -> None:
    register_user(db_session, make_registration())

    duplicate = make_registration(
        username="another_user",
        phone_number="+254 712 345 678",
    )

    with pytest.raises(RegistrationConflictError):
        register_user(db_session, duplicate)


def test_register_user_uses_generic_duplicate_error(
    db_session: Session,
) -> None:
    register_user(db_session, make_registration())

    duplicate = make_registration(
        username="test_user",
        phone_number="0712345678",
    )

    with pytest.raises(
        RegistrationConflictError,
        match="An account with these details already exists.",
    ):
        register_user(db_session, duplicate)


def test_register_user_does_not_store_plaintext_password(
    db_session: Session,
) -> None:
    password = "a-secure-password-123"

    user = register_user(
        db_session,
        make_registration(password=password),
    )

    assert password not in user.password_hash


def test_registration_does_not_commit_transaction(
    db_session: Session,
) -> None:
    register_user(db_session, make_registration())

    count = db_session.scalar(select(func.count()).select_from(User))

    assert count == 1
