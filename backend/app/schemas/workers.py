from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class WorkerProfileUpdate(BaseModel):
    """Validate fields that a worker can update on their own profile."""

    model_config = ConfigDict(extra="forbid")

    display_name: str | None = Field(default=None, min_length=1, max_length=120)
    bio: str | None = None
    service_description: str | None = None
    experience_years: int | None = Field(default=None, ge=0, strict=True)
    availability: Literal["available", "busy", "unavailable"] | None = None

    @field_validator("display_name", mode="before")
    @classmethod
    def validate_display_name(cls, value: object) -> object:
        """Require a non-null display name and remove surrounding whitespace."""
        if value is None:
            raise ValueError("Display name cannot be null.")
        if isinstance(value, str):
            return value.strip()
        return value

    @field_validator("availability", mode="before")
    @classmethod
    def validate_availability(cls, value: object) -> object:
        """Require a non-null availability value."""
        if value is None:
            raise ValueError("Availability cannot be null.")
        return value

    @field_validator("bio", "service_description", mode="before")
    @classmethod
    def normalize_optional_text(cls, value: object) -> object:
        """Normalize blank optional text to null."""
        if isinstance(value, str):
            return value.strip() or None
        return value

    @model_validator(mode="after")
    def validate_non_empty_update(self) -> "WorkerProfileUpdate":
        """Reject requests that do not contain an editable field."""
        if not self.model_fields_set:
            raise ValueError("At least one profile field must be provided.")
        return self


class WorkerProfileRead(BaseModel):
    """Expose safe worker profile information."""

    model_config = ConfigDict(from_attributes=True, extra="forbid")

    id: UUID
    user_id: UUID
    display_name: str
    bio: str | None
    service_description: str | None
    experience_years: int | None
    availability: Literal["available", "busy", "unavailable"]
    created_at: datetime
    updated_at: datetime
