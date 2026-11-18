import re

import pytest

from app.core.security import (
    generate_session_token,
    hash_session_token,
)


def test_session_token_is_generated() -> None:
    token = generate_session_token()

    assert isinstance(token, str)
    assert len(token) >= 40


def test_session_tokens_are_unique() -> None:
    tokens = {generate_session_token() for _ in range(100)}

    assert len(tokens) == 100


def test_session_token_uses_url_safe_characters() -> None:
    token = generate_session_token()

    assert re.fullmatch(r"[A-Za-z0-9_-]+", token)


def test_session_token_hash_is_sha256() -> None:
    token = generate_session_token()
    token_hash = hash_session_token(token)

    assert len(token_hash) == 64
    assert re.fullmatch(r"[0-9a-f]{64}", token_hash)


def test_same_session_token_produces_same_hash() -> None:
    token = generate_session_token()

    assert hash_session_token(token) == hash_session_token(token)


def test_different_session_tokens_produce_different_hashes() -> None:
    first_token = generate_session_token()
    second_token = generate_session_token()

    assert hash_session_token(first_token) != hash_session_token(second_token)


@pytest.mark.parametrize("token", ["", None])
def test_empty_or_missing_session_tokens_are_rejected(token: str | None) -> None:
    with pytest.raises(ValueError):
        hash_session_token(token)
