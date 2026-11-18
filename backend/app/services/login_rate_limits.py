from datetime import UTC, datetime, timedelta
from ipaddress import ip_address
from math import ceil

from sqlalchemy import and_, case, delete, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.security import hash_login_rate_limit_key
from app.models.login_rate_limit import LoginRateLimit


class LoginRateLimitExceeded(Exception):
    """Raised when a login identifier or IP address is rate limited."""

    def __init__(self, retry_after_seconds: int) -> None:
        self.retry_after_seconds = retry_after_seconds
        super().__init__("Login attempts are temporarily rate limited.")


def _current_time(now: datetime | None = None) -> datetime:
    """Return a timezone-aware UTC timestamp."""
    current = now or datetime.now(UTC)

    if current.tzinfo is None or current.utcoffset() is None:
        raise ValueError("The timestamp must be timezone-aware.")

    return current.astimezone(UTC)


def _rate_limit_keys(username: str, client_ip: str) -> tuple[str, str]:
    """Build HMAC identifiers for a username/IP pair and an IP address."""
    if not isinstance(username, str) or not username.strip():
        raise ValueError("Username must be non-empty.")

    if not isinstance(client_ip, str) or not client_ip.strip():
        raise ValueError("Client IP address must be non-empty.")

    normalized_username = username.strip().casefold()
    normalized_ip = ip_address(client_ip.strip()).compressed

    secret = settings.auth_secret_key.get_secret_value()

    username_ip_key = hash_login_rate_limit_key(
        secret,
        "username_ip",
        f"{normalized_username}:{normalized_ip}",
    )

    ip_key = hash_login_rate_limit_key(
        secret,
        "ip",
        normalized_ip,
    )

    return username_ip_key, ip_key


def check_login_rate_limit(
    db: Session,
    username: str,
    client_ip: str,
    now: datetime | None = None,
) -> None:
    """Reject login attempts when either rate-limit scope is blocked."""
    current = _current_time(now)
    username_ip_key, ip_key = _rate_limit_keys(username, client_ip)

    statement = select(LoginRateLimit.blocked_until).where(
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

    blocked_until_values = db.scalars(statement).all()

    active_blocks = [
        blocked_until
        for blocked_until in blocked_until_values
        if blocked_until is not None and blocked_until > current
    ]

    if active_blocks:
        retry_after = max(
            ceil((blocked_until - current).total_seconds()) for blocked_until in active_blocks
        )
        raise LoginRateLimitExceeded(max(1, retry_after))


def _record_attempt(
    db: Session,
    *,
    scope: str,
    key_hash: str,
    limit: int,
    now: datetime,
    window: timedelta,
) -> None:
    """Atomically increment or reset one PostgreSQL rate-limit counter."""
    table = LoginRateLimit.__table__
    cutoff = now - window

    expired_window = table.c.window_started_at <= cutoff

    next_attempts = case(
        (expired_window, 1),
        else_=table.c.attempts + 1,
    )

    next_window_started_at = case(
        (expired_window, now),
        else_=table.c.window_started_at,
    )

    next_blocked_until = case(
        (expired_window, None),
        (
            table.c.attempts + 1 >= limit,
            table.c.window_started_at + window,
        ),
        else_=table.c.blocked_until,
    )

    statement = insert(LoginRateLimit).values(
        scope=scope,
        key_hash=key_hash,
        window_started_at=now,
        attempts=1,
        blocked_until=None,
        updated_at=now,
    )

    statement = statement.on_conflict_do_update(
        index_elements=[
            table.c.scope,
            table.c.key_hash,
        ],
        set_={
            "attempts": next_attempts,
            "window_started_at": next_window_started_at,
            "blocked_until": next_blocked_until,
            "updated_at": now,
        },
    )

    db.execute(statement)


def record_failed_login(
    db: Session,
    username: str,
    client_ip: str,
    now: datetime | None = None,
) -> None:
    """Record a failed login against both configured rate-limit scopes."""
    current = _current_time(now)
    username_ip_key, ip_key = _rate_limit_keys(username, client_ip)

    window = timedelta(minutes=settings.login_rate_limit_window_minutes)

    # Lock the IP-wide counter first, then the username/IP counter.
    _record_attempt(
        db,
        scope="ip",
        key_hash=ip_key,
        limit=settings.login_rate_limit_ip_attempts,
        now=current,
        window=window,
    )

    _record_attempt(
        db,
        scope="username_ip",
        key_hash=username_ip_key,
        limit=settings.login_rate_limit_username_ip_attempts,
        now=current,
        window=window,
    )


def reset_username_ip_rate_limit(
    db: Session,
    username: str,
    client_ip: str,
) -> None:
    """Reset the username/IP counter after successful authentication."""
    username_ip_key, _ = _rate_limit_keys(username, client_ip)

    db.execute(
        delete(LoginRateLimit).where(
            LoginRateLimit.scope == "username_ip",
            LoginRateLimit.key_hash == username_ip_key,
        )
    )
