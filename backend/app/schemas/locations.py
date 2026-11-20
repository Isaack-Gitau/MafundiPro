from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator


class LocationSchema(BaseModel):
    """Base configuration for location request and response schemas."""

    model_config = ConfigDict(
        extra="forbid",
        from_attributes=True,
    )


class LocationCreate(LocationSchema):
    """Validate location creation."""

    location_type: str = Field(min_length=1, max_length=40)
    name: str = Field(min_length=1, max_length=120)
    parent_id: UUID | None = None
    country_code: str | None = Field(default=None, max_length=2)
    description: str | None = Field(default=None, max_length=2000)
    latitude: Decimal | None = Field(default=None, ge=-90, le=90)
    longitude: Decimal | None = Field(default=None, ge=-180, le=180)
    display_order: int = Field(default=0, ge=0, strict=True)

    @model_validator(mode="after")
    def validate_coordinates(self) -> LocationCreate:
        if (self.latitude is None) != (self.longitude is None):
            raise ValueError("Latitude and longitude must be provided together.")

        return self


class LocationUpdate(LocationSchema):
    """Validate partial location updates."""

    name: str | None = Field(default=None, min_length=1, max_length=120)
    country_code: str | None = Field(default=None, max_length=2)
    description: str | None = Field(default=None, max_length=2000)
    latitude: Decimal | None = Field(default=None, ge=-90, le=90)
    longitude: Decimal | None = Field(default=None, ge=-180, le=180)
    display_order: int | None = Field(default=None, ge=0, strict=True)

    @model_validator(mode="after")
    def validate_explicit_values(self) -> LocationUpdate:
        if "name" in self.model_fields_set and self.name is None:
            raise ValueError("Location name cannot be null.")

        if "display_order" in self.model_fields_set and self.display_order is None:
            raise ValueError("Display order cannot be null.")

        if "country_code" in self.model_fields_set and self.country_code is None:
            raise ValueError("Country code cannot be null.")

        latitude_supplied = "latitude" in self.model_fields_set
        longitude_supplied = "longitude" in self.model_fields_set

        if latitude_supplied != longitude_supplied:
            raise ValueError("Latitude and longitude must be updated together.")

        return self


class LocationMove(LocationSchema):
    """Validate a location hierarchy move."""

    parent_id: UUID | None


class LocationActivation(LocationSchema):
    """Validate location activation changes."""

    is_active: bool = Field(strict=True)


class LocationRead(LocationSchema):
    """Represent a location in administrative API responses."""

    id: UUID
    parent_id: UUID | None
    location_type: str
    name: str
    country_code: str | None
    description: str | None
    latitude: Decimal | None
    longitude: Decimal | None
    display_order: int
    is_active: bool
    created_at: datetime
    updated_at: datetime


class LocationTreeRead(LocationRead):
    """Represent an administrative location with nested children."""

    children: list[LocationTreeRead] = Field(default_factory=list)


class PublicLocationRead(LocationSchema):
    """Expose safe, provider-independent location information publicly."""

    id: UUID
    parent_id: UUID | None
    location_type: str
    name: str
    country_code: str | None
    description: str | None
    display_order: int


class PublicLocationTreeRead(PublicLocationRead):
    """Represent a public location with its visible descendants."""

    children: list[PublicLocationTreeRead] = Field(default_factory=list)
