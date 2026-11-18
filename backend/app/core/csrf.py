from __future__ import annotations

import hashlib
import hmac
import secrets
from urllib.parse import urlsplit

from app.core.config import settings
from app.core.security import hash_session_token


class InvalidCSRFTokenError(Exception):
    """Raised when a CSRF token is invalid or belongs to another session."""


class InvalidOriginError(Exception):
    """Raised when a browser request has an untrusted Origin header."""


def _csrf_signature(session_token_hash: str, nonce: str) -> str:
    """Create a session-bound HMAC-SHA256 signature."""
    secret = settings.auth_secret_key.get_secret_value()

    message = (f"mafundipro:csrf:{session_token_hash}:{nonce}").encode()

    return hmac.new(
        secret.encode("utf-8"),
        message,
        hashlib.sha256,
    ).hexdigest()


def generate_csrf_token(session_token: str) -> str:
    """Generate a signed CSRF token bound to an authenticated session."""
    if not isinstance(session_token, str) or not session_token:
        raise ValueError("A valid session token is required.")

    session_token_hash = hash_session_token(session_token)
    nonce = secrets.token_urlsafe(32)
    signature = _csrf_signature(session_token_hash, nonce)

    return f"{nonce}.{signature}"


def verify_csrf_token(session_token: str, csrf_token: str) -> bool:
    """Verify a CSRF token against the session that originally received it."""
    if not isinstance(session_token, str) or not session_token or not isinstance(csrf_token, str):
        return False

    parts = csrf_token.split(".")

    if len(parts) != 2:
        return False

    nonce, supplied_signature = parts

    if not nonce or not supplied_signature:
        return False

    expected_signature = _csrf_signature(
        hash_session_token(session_token),
        nonce,
    )

    return hmac.compare_digest(
        supplied_signature,
        expected_signature,
    )


def _normalize_origin(origin: str) -> str | None:
    """Validate and normalize an HTTP or HTTPS origin."""
    try:
        parsed = urlsplit(origin)

        if parsed.scheme not in {"http", "https"}:
            return None

        if not parsed.hostname:
            return None

        if parsed.username is not None or parsed.password is not None:
            return None

        if parsed.path or parsed.query or parsed.fragment:
            return None

        if origin != origin.strip():
            return None

        if parsed.port is not None and not 1 <= parsed.port <= 65535:
            return None

        hostname = parsed.hostname.lower()

        if ":" in hostname:
            hostname = f"[{hostname}]"

        default_port = (parsed.scheme == "http" and parsed.port == 80) or (
            parsed.scheme == "https" and parsed.port == 443
        )

        if parsed.port is None or default_port:
            return f"{parsed.scheme}://{hostname}"

        return f"{parsed.scheme}://{hostname}:{parsed.port}"

    except ValueError:
        return None


def validate_origin(origin: str | None) -> None:
    """Reject browser requests whose Origin is not explicitly trusted."""
    if origin is None:
        return

    normalized_origin = _normalize_origin(origin)

    if normalized_origin is None:
        raise InvalidOriginError("The request origin is invalid.")

    allowed_origins = {
        normalized
        for configured in settings.allowed_frontend_origins
        if (normalized := _normalize_origin(configured)) is not None
    }

    if normalized_origin not in allowed_origins:
        raise InvalidOriginError("The request origin is not trusted.")
