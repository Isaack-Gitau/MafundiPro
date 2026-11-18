import hashlib
import hmac
import secrets

from argon2 import PasswordHasher, Type
from argon2.exceptions import (
    InvalidHashError,
    VerificationError,
    VerifyMismatchError,
)

_password_hasher = PasswordHasher(
    type=Type.ID,
)


def hash_password(password: str) -> str:
    """Hash a plaintext password using Argon2id."""
    if not isinstance(password, str) or not password:
        raise ValueError("Password must be a non-empty string.")

    if len(password.encode("utf-8")) > 1024:
        raise ValueError("Password exceeds the maximum supported length.")

    return _password_hasher.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    """Verify a password against an existing Argon2id hash."""
    if not isinstance(password, str) or not isinstance(password_hash, str):
        return False

    try:
        return _password_hasher.verify(password_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def password_needs_rehash(password_hash: str) -> bool:
    """Check whether a stored password hash needs updated parameters."""
    try:
        return _password_hasher.check_needs_rehash(password_hash)
    except (VerificationError, InvalidHashError):
        return False


def generate_session_token() -> str:
    """Generate a cryptographically secure session token."""
    return secrets.token_urlsafe(32)


def hash_session_token(token: str) -> str:
    """Return the SHA-256 digest of the session token for database storage."""
    if not isinstance(token, str) or not token:
        raise ValueError("Session token must be a non-empty string.")

    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def hash_login_rate_limit_key(
    secret_key: str,
    scope: str,
    identifier: str,
) -> str:
    """Return a scope-separated HMAC-SHA256 login rate-limit identifier."""
    if not isinstance(secret_key, str) or not secret_key:
        raise ValueError("The HMAC secret must be a non-empty string.")

    if scope not in {"username_ip", "ip"}:
        raise ValueError("Unsupported login rate-limit scope.")

    if not isinstance(identifier, str) or not identifier:
        raise ValueError("The rate-limit identifier must be non-empty.")

    message = f"mafundipro:login-rate-limit:{scope}:{identifier}".encode()

    return hmac.new(
        secret_key.encode("utf-8"),
        message,
        hashlib.sha256,
    ).hexdigest()
