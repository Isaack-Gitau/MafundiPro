import pytest
from pydantic import SecretStr

from app.core.config import settings
from app.core.csrf import (
    InvalidOriginError,
    generate_csrf_token,
    validate_origin,
    verify_csrf_token,
)


@pytest.fixture(autouse=True)
def test_security_settings(monkeypatch):
    monkeypatch.setattr(
        settings,
        "auth_secret_key",
        SecretStr("test-only-csrf-secret-key"),
    )
    monkeypatch.setattr(
        settings,
        "frontend_origins",
        "http://localhost:5173,https://mafundipro.example",
    )


def test_generated_csrf_token_is_valid_for_its_session():
    session_token = "test-session-token"

    csrf_token = generate_csrf_token(session_token)

    assert verify_csrf_token(session_token, csrf_token)


def test_csrf_token_is_rejected_for_another_session():
    csrf_token = generate_csrf_token("first-session")

    assert not verify_csrf_token("second-session", csrf_token)


def test_tampered_csrf_token_is_rejected():
    csrf_token = generate_csrf_token("test-session-token")
    nonce, signature = csrf_token.split(".")
    replacement = "0" if signature[-1] != "0" else "1"
    tampered_token = f"{nonce}.{signature[:-1]}{replacement}"

    assert not verify_csrf_token("test-session-token", tampered_token)


@pytest.mark.parametrize(
    "csrf_token",
    [
        "",
        "invalid",
        "one.two.three",
        ".signature",
        "nonce.",
        None,
    ],
)
def test_malformed_csrf_tokens_are_rejected(csrf_token):
    assert not verify_csrf_token("test-session-token", csrf_token)


@pytest.mark.parametrize(
    ("origin", "expected"),
    [
        ("http://localhost:5173", True),
        ("http://localhost:5173/", False),
        ("http://localhost:5173:80", False),
        ("https://mafundipro.example", True),
        ("https://mafundipro.example:443", True),
        ("https://evil.example", False),
        ("null", False),
        ("file://localhost", False),
        ("https://mafundipro.example/path", False),
        ("https://user@mafundipro.example", False),
    ],
)
def test_origin_validation(origin, expected):
    if expected:
        validate_origin(origin)
    else:
        with pytest.raises(InvalidOriginError):
            validate_origin(origin)


def test_missing_origin_is_allowed_for_native_clients():
    validate_origin(None)


def test_untrusted_origin_is_rejected():
    with pytest.raises(InvalidOriginError):
        validate_origin("https://attacker.example")
