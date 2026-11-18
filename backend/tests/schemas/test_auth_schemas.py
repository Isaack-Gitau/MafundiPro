from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.schemas.auth import LoginCreate, LoginRead, RegistrationCreate


def valid_registration(**overrides) -> dict[str, str]:
    values = {
        "full_name": "Isaack Gitau",
        "username": "Isaack_Gitau",
        "phone_number": "0712345678",
        "password": "a-secure-password-123",
    }
    values.update(overrides)
    return values


def valid_login(**overrides) -> dict[str, str]:
    values = {
        "username": "Isaack_Gitau",
        "password": "a-secure-password-123",
    }
    values.update(overrides)
    return values


def test_registration_accepts_valid_details() -> None:
    registration = RegistrationCreate(**valid_registration())

    assert registration.full_name == "Isaack Gitau"
    assert registration.username == "Isaack_Gitau"
    assert registration.phone_number == "+254712345678"
    assert registration.password == "a-secure-password-123"


@pytest.mark.parametrize(
    ("phone_number", "expected"),
    [
        ("0712345678", "+254712345678"),
        ("0112345678", "+254112345678"),
        ("+254712345678", "+254712345678"),
        ("+254112345678", "+254112345678"),
        ("254712345678", "+254712345678"),
        ("(071) 234-5678", "+254712345678"),
        (" 0712345678 ", "+254712345678"),
    ],
)
def test_registration_normalizes_kenyan_phone_numbers(
    phone_number: str,
    expected: str,
) -> None:
    registration = RegistrationCreate(**valid_registration(phone_number=phone_number))

    assert registration.phone_number == expected


@pytest.mark.parametrize(
    "phone_number",
    [
        "",
        "071234567",
        "0612345678",
        "0812345678",
        "+254612345678",
        "+25471234567",
        "+2547123456789",
        "1234567890",
        "25471234567",
    ],
)
def test_registration_rejects_invalid_phone_numbers(phone_number: str) -> None:
    with pytest.raises(ValidationError):
        RegistrationCreate(**valid_registration(phone_number=phone_number))


@pytest.mark.parametrize(
    "username",
    [
        "ab",
        "a" * 31,
        "user name",
        "user@name",
        "user/name",
        "",
    ],
)
def test_registration_rejects_invalid_usernames(username: str) -> None:
    with pytest.raises(ValidationError):
        RegistrationCreate(**valid_registration(username=username))


def test_registration_preserves_username_casing() -> None:
    registration = RegistrationCreate(**valid_registration(username="Isaack.G"))

    assert registration.username == "Isaack.G"


@pytest.mark.parametrize(
    "full_name",
    [
        "",
        " ",
        "A",
        "a" * 161,
    ],
)
def test_registration_rejects_invalid_full_names(full_name: str) -> None:
    with pytest.raises(ValidationError):
        RegistrationCreate(**valid_registration(full_name=full_name))


def test_registration_normalizes_full_name_whitespace() -> None:
    registration = RegistrationCreate(**valid_registration(full_name="  Isaack    Gitau  "))

    assert registration.full_name == "Isaack Gitau"


@pytest.mark.parametrize(
    "password",
    [
        "",
        "short",
        "12345678901",
    ],
)
def test_registration_rejects_short_passwords(password: str) -> None:
    with pytest.raises(ValidationError):
        RegistrationCreate(**valid_registration(password=password))


def test_registration_rejects_password_exceeding_byte_limit() -> None:
    password = "Ã©" * 513

    with pytest.raises(ValidationError):
        RegistrationCreate(**valid_registration(password=password))


def test_registration_does_not_modify_password() -> None:
    password = "  secure password  "
    registration = RegistrationCreate(**valid_registration(password=password))

    assert registration.password == password


def test_registration_rejects_unknown_fields() -> None:
    values = valid_registration()
    values["is_admin"] = "true"

    with pytest.raises(ValidationError):
        RegistrationCreate(**values)


def test_login_accepts_valid_credentials() -> None:
    login = LoginCreate(**valid_login())

    assert login.username == "Isaack_Gitau"
    assert login.password == "a-secure-password-123"


def test_login_normalizes_username_whitespace() -> None:
    login = LoginCreate(**valid_login(username="  Isaack_Gitau  "))

    assert login.username == "Isaack_Gitau"


def test_login_preserves_username_casing() -> None:
    login = LoginCreate(**valid_login(username="Isaack.G"))

    assert login.username == "Isaack.G"


@pytest.mark.parametrize(
    "username",
    [
        "",
        "ab",
        "a" * 31,
        "user name",
        "user@name",
        "user/name",
    ],
)
def test_login_rejects_invalid_usernames(username: str) -> None:
    with pytest.raises(ValidationError):
        LoginCreate(**valid_login(username=username))


@pytest.mark.parametrize(
    "password",
    [
        "",
        "Ã©" * 513,
    ],
)
def test_login_rejects_invalid_passwords(password: str) -> None:
    with pytest.raises(ValidationError):
        LoginCreate(**valid_login(password=password))


def test_login_preserves_password_exactly() -> None:
    password = "  secure password  "
    login = LoginCreate(**valid_login(password=password))

    assert login.password == password


def test_login_rejects_unknown_fields() -> None:
    values = valid_login()
    values["is_admin"] = "true"

    with pytest.raises(ValidationError):
        LoginCreate(**values)


def test_login_response_contains_only_safe_fields() -> None:
    response = LoginRead(
        id=uuid4(),
        full_name="Test User",
        username="test_user",
        csrf_token="test-csrf-token",
    )

    assert set(response.model_dump()) == {
        "id",
        "full_name",
        "username",
        "csrf_token",
    }
