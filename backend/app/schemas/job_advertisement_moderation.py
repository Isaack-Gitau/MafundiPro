from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator


class JobAdvertisementModerationReason(BaseModel):
    """Required reason for a moderation action."""

    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=1, max_length=2000)

    @field_validator("reason")
    @classmethod
    def normalize_reason(cls, value: str) -> str:
        """Reject whitespace-only reasons and remove surrounding whitespace."""
        normalized = value.strip()

        if not normalized:
            raise ValueError("A moderation reason is required.")

        return normalized


class JobAdvertisementModerationRead(BaseModel):
    """Result of an advertisement moderation action."""

    model_config = ConfigDict(extra="forbid")

    id: UUID
    status: Literal["published", "suspended", "removed"]
    reason: str
    published_at: datetime | None
    expires_at: datetime | None
    updated_at: datetime
