from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

EmployerType = Literal["individual", "business", "organization", "contractor"]


class EmployerProfileCreate(BaseModel):
    """Validate the information required to create an employer profile."""

    model_config = ConfigDict(extra="forbid")

    employer_type: EmployerType
    description: str | None = None

    @field_validator("description", mode="before")
    @classmethod
    def normalize_description(cls, value: object) -> object:
        """Normalize blank descriptions to null."""
        if isinstance(value, str):
            return value.strip() or None
        return value


class EmployerProfileUpdate(BaseModel):
    """Validate fields an employer can update on their own profile."""

    model_config = ConfigDict(extra="forbid")

    employer_type: EmployerType | None = None
    display_name: str | None = Field(default=None, min_length=1, max_length=120)
    description: str | None = None

    @field_validator("employer_type", "display_name", mode="before")
    @classmethod
    def reject_null_required_values(cls, value: object) -> object:
        """Prevent explicitly setting required profile fields to null."""
        if value is None:
            raise ValueError("This profile field cannot be null.")
        return value

    @field_validator("display_name", mode="before")
    @classmethod
    def normalize_display_name(cls, value: object) -> object:
        """Remove surrounding whitespace from display names."""
        if isinstance(value, str):
            return value.strip()
        return value

    @field_validator("description", mode="before")
    @classmethod
    def normalize_description(cls, value: object) -> object:
        """Normalize blank descriptions to null."""
        if isinstance(value, str):
            return value.strip() or None
        return value

    @model_validator(mode="after")
    def validate_non_empty_update(self) -> "EmployerProfileUpdate":
        """Reject requests without an editable field."""
        if not self.model_fields_set:
            raise ValueError("At least one profile field must be provided.")
        return self


class EmployerProfileRead(BaseModel):
    """Expose safe employer profile information."""

    model_config = ConfigDict(from_attributes=True, extra="forbid")

    id: UUID
    user_id: UUID
    employer_type: EmployerType
    display_name: str
    description: str | None
    created_at: datetime
    updated_at: datetime
