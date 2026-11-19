from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class CurrentUserRead(BaseModel):
    """Expose safe information about the authenticated account."""

    model_config = ConfigDict(from_attributes=True, extra="forbid")

    id: UUID
    full_name: str
    username: str
    phone_number: str
    account_status: Literal["active"]
    phone_verified_at: datetime
    created_at: datetime
    updated_at: datetime
    platform_roles: list[Literal["admin", "moderator"]]
