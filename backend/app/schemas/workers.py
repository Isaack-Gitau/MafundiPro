from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict


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
