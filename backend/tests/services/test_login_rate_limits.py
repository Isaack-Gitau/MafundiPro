from collections.abc import Generator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import and_, create_engine, delete, or_, select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.security import hash_login_rate_limit_key
from app.models.login_rate_limit import LoginRateLimit
from app.services.login_rate_limits import (
    LoginRateLimitExceeded,
    check_login_rate_limit,
    record_failed_login,
    reset_username_ip_rate_limit,
)


@pytest.fixture(scope="session")
def test_engine():
    """Connect exclusively to the isolated PostgreSQL test database."""
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
    """Provide a rollback-isolated database session."""
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


def test_username_ip_limit_blocks_after_ten_failures(
    db_session: Session,
) -> None:
    now = datetime(2026, 11, 19, 12, 0, tzinfo=UTC)

    for _ in range(10):
        record_failed_login(
            db_session,
            "worker_test",
            "192.0.2.10",
            now=now,
        )

    with pytest.raises(LoginRateLimitExceeded) as exc_info:
        check_login_rate_limit(
            db_session,
            "worker_test",
            "192.0.2.10",
            now=now,
        )

    assert exc_info.value.retry_after_seconds == 900


def test_ip_limit_blocks_across_different_usernames(
    db_session: Session,
) -> None:
    now = datetime(2026, 11, 19, 12, 0, tzinfo=UTC)
    client_ip = "192.0.2.20"

    for attempt in range(30):
        record_failed_login(
            db_session,
            f"worker_{attempt}",
            client_ip,
            now=now,
        )

    with pytest.raises(LoginRateLimitExceeded):
        check_login_rate_limit(
            db_session,
            "another_worker",
            client_ip,
            now=now,
        )


def test_rate_limit_window_expires_after_fifteen_minutes(
    db_session: Session,
) -> None:
    now = datetime(2026, 11, 19, 12, 0, tzinfo=UTC)
    username = "window_test"
    client_ip = "192.0.2.30"

    for _ in range(10):
        record_failed_login(
            db_session,
            username,
            client_ip,
            now=now,
        )

    after_window = now + timedelta(minutes=15)

    check_login_rate_limit(
        db_session,
        username,
        client_ip,
        now=after_window,
    )

    record_failed_login(
        db_session,
        username,
        client_ip,
        now=after_window,
    )

    username_ip_key = hash_login_rate_limit_key(
        settings.auth_secret_key.get_secret_value(),
        "username_ip",
        f"{username}:{client_ip}",
    )

    row = db_session.scalar(
        select(LoginRateLimit).where(
            LoginRateLimit.scope == "username_ip",
            LoginRateLimit.key_hash == username_ip_key,
        )
    )

    assert row is not None
    assert row.attempts == 1
    assert row.window_started_at == after_window
    assert row.blocked_until is None


def test_successful_login_resets_only_username_ip_counter(
    db_session: Session,
) -> None:
    now = datetime(2026, 11, 19, 12, 0, tzinfo=UTC)
    username = "successful_login"
    client_ip = "192.0.2.40"

    for _ in range(3):
        record_failed_login(
            db_session,
            username,
            client_ip,
            now=now,
        )

    reset_username_ip_rate_limit(
        db_session,
        username,
        client_ip,
    )

    username_ip_key = hash_login_rate_limit_key(
        settings.auth_secret_key.get_secret_value(),
        "username_ip",
        f"{username}:{client_ip}",
    )

    ip_key = hash_login_rate_limit_key(
        settings.auth_secret_key.get_secret_value(),
        "ip",
        client_ip,
    )

    username_row = db_session.scalar(
        select(LoginRateLimit).where(
            LoginRateLimit.scope == "username_ip",
            LoginRateLimit.key_hash == username_ip_key,
        )
    )

    ip_row = db_session.scalar(
        select(LoginRateLimit).where(
            LoginRateLimit.scope == "ip",
            LoginRateLimit.key_hash == ip_key,
        )
    )

    assert username_row is None
    assert ip_row is not None
    assert ip_row.attempts == 3


def test_concurrent_failures_are_counted_atomically(
    test_engine,
) -> None:
    username = f"concurrent_{uuid4().hex}"
    client_ip = "198.51.100.25"
    now = datetime(2026, 11, 19, 12, 0, tzinfo=UTC)

    secret = settings.auth_secret_key.get_secret_value()

    username_ip_key = hash_login_rate_limit_key(
        secret,
        "username_ip",
        f"{username}:{client_ip}",
    )
    ip_key = hash_login_rate_limit_key(
        secret,
        "ip",
        client_ip,
    )

    def record_attempt(_: int) -> None:
        with Session(test_engine) as session:
            record_failed_login(
                session,
                username,
                client_ip,
                now=now,
            )
            session.commit()

    try:
        with ThreadPoolExecutor(max_workers=5) as executor:
            list(executor.map(record_attempt, range(20)))

        with Session(test_engine) as session:
            rows = session.scalars(
                select(LoginRateLimit).where(
                    or_(
                        and_(
                            LoginRateLimit.scope == "username_ip",
                            LoginRateLimit.key_hash == username_ip_key,
                        ),
                        and_(
                            LoginRateLimit.scope == "ip",
                            LoginRateLimit.key_hash == ip_key,
                        ),
                    )
                )
            ).all()

            attempts_by_scope = {row.scope: row.attempts for row in rows}

        assert attempts_by_scope == {
            "username_ip": 20,
            "ip": 20,
        }

    finally:
        with test_engine.begin() as connection:
            connection.execute(
                delete(LoginRateLimit).where(
                    or_(
                        and_(
                            LoginRateLimit.scope == "username_ip",
                            LoginRateLimit.key_hash == username_ip_key,
                        ),
                        and_(
                            LoginRateLimit.scope == "ip",
                            LoginRateLimit.key_hash == ip_key,
                        ),
                    )
                )
            )


def test_rate_limit_rejects_naive_timestamp(
    db_session: Session,
) -> None:
    naive_time = datetime(2026, 11, 19, 12, 0)

    with pytest.raises(ValueError, match="timezone-aware"):
        check_login_rate_limit(
            db_session,
            "timestamp_test",
            "192.0.2.50",
            now=naive_time,
        )
