import pytest

from app.core.security import (
    hash_password,
    password_needs_rehash,
    verify_password,
)


def test_password_is_hashed_with_argon2id() -> None:
    password_hash = hash_password("Correct-Horse-Battery-2026!")

    assert password_hash.startswith("$argon2id$")
    assert password_hash != "Correct-Horse-Battery-2026!"


def test_identical_passwords_produce_different_hashes() -> None:
    first_hash = hash_password("Correct-Horse-Battery-2026!")
    second_hash = hash_password("Correct-Horse-Battery-2026!")

    assert first_hash != second_hash


def test_correct_password_is_verified() -> None:
    password = "Correct-Horse-Battery-2026!"
    password_hash = hash_password(password)

    assert verify_password(password, password_hash) is True


def test_incorrect_password_is_rejected() -> None:
    password_hash = hash_password("Correct-Horse-Battery-2026!")

    assert verify_password("Wrong-Password-2026!", password_hash) is False


@pytest.mark.parametrize(
    ("password", "password_hash"),
    [
        ("password", "not-a-valid-hash"),
        ("password", ""),
        ("", "not-a-valid-hash"),
        (None, "not-a-valid-hash"),
        ("password", None),
    ],
)
def test_invalid_verification_inputs_are_rejected(
    password: str | None,
    password_hash: str | None,
) -> None:
    assert verify_password(password, password_hash) is False


@pytest.mark.parametrize(
    "password",
    [
        "",
        "a" * 1025,
    ],
)
def test_invalid_passwords_cannot_be_hashed(password: str) -> None:
    with pytest.raises(ValueError):
        hash_password(password)


def test_current_password_hash_does_not_need_rehashing() -> None:
    password_hash = hash_password("Correct-Horse-Battery-2026!")

    assert password_needs_rehash(password_hash) is False


def test_malformed_hash_does_not_raise_during_rehash_check() -> None:
    assert password_needs_rehash("not-a-valid-hash") is False
