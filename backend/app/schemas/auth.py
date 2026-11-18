import re
from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator


class AuthSchema(BaseModel):
    """Base configuration for authentication schemas."""

    model_config = ConfigDict(extra="forbid")


class RegistrationCreate(AuthSchema):
    """Validate a new MafundiPro account registration."""

    full_name: str = Field(max_length=160)
    username: str
    phone_number: str
    password: str

    @field_validator("full_name")
    @classmethod
    def validate_full_name(cls, value: str) -> str:
        value = re.sub(r"\s+", " ", value).strip()

        if not 2 <= len(value) <= 160:
            raise ValueError("Full name must contain between 2 and 160 characters.")

        return value

    @field_validator("username")
    @classmethod
    def validate_username(cls, value: str) -> str:
        value = value.strip()

        if not 3 <= len(value) <= 30:
            raise ValueError("Username must contain between 3 and 30 characters.")

        if not re.fullmatch(r"[A-Za-z0-9_.]+", value):
            raise ValueError(
                "Username may contain only letters, numbers, underscores, and periods."
            )

        return value

    @field_validator("phone_number")
    @classmethod
    def normalize_phone_number(cls, value: str) -> str:
        value = re.sub(r"[\s()-]", "", value)

        if value.startswith("0") and len(value) == 10:
            value = "+254" + value[1:]
        elif value.startswith("254") and len(value) == 12:
            value = "+" + value

        if not re.fullmatch(r"\+254[17]\d{8}", value):
            raise ValueError("Enter a valid Kenyan mobile phone number.")

        return value

    @field_validator("password")
    @classmethod
    def validate_password(cls, value: str) -> str:
        if len(value) < 12:
            raise ValueError("Password must contain at least 12 characters.")

        if len(value.encode("utf-8")) > 1024:
            raise ValueError("Password exceeds the maximum supported length.")

        return value


class RegistrationRead(BaseModel):
    """Expose only safe account information after registration."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    full_name: str
    username: str
    phone_number: str
    account_status: Literal["pending_verification"]
    created_at: datetime


class LoginCreate(AuthSchema):
    """Validate login credentials."""

    username: str
    password: str

    @field_validator("username")
    @classmethod
    def validate_username(cls, value: str) -> str:
        value = value.strip()

        if not 3 <= len(value) <= 30:
            raise ValueError("Username must contain between 3 and 30 characters.")

        if not re.fullmatch(r"[A-Za-z0-9_.]+", value):
            raise ValueError("Enter a valid username.")

        return value

    @field_validator("password")
    @classmethod
    def validate_password(cls, value: str) -> str:
        if not value:
            raise ValueError("Password is required.")

        if len(value.encode("utf-8")) > 1024:
            raise ValueError("Password exceeds the maximum supported length.")

        return value


class LoginRead(BaseModel):
    """Expose safe account information and the session-bound CSRF token."""

    id: UUID
    full_name: str
    username: str
    csrf_token: str
