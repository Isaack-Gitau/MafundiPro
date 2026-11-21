from datetime import date, datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.job_advertisements import (
    CompensationType,
    ContactMethod,
    JobType,
    PaymentUnit,
    WorkArrangement,
)


class EmployerAdvertisementInput(BaseModel):
    """Fields an employer may supply when creating or editing an advertisement."""

    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(default=None, max_length=160)
    description: str | None = None
    job_type: JobType | None = None
    work_arrangement: WorkArrangement | None = None

    main_category_id: UUID | None = None
    subcategory_id: UUID | None = None
    skill_ids: list[UUID] | None = None
    location_id: UUID | None = None

    compensation_type: CompensationType | None = None
    min_compensation: Decimal | None = Field(default=None, ge=0)
    max_compensation: Decimal | None = Field(default=None, ge=0)
    payment_unit: PaymentUnit | None = None
    currency_code: str | None = Field(default=None, min_length=3, max_length=3)

    workers_needed: int | None = Field(default=None, ge=1)
    start_date: date | None = None
    response_deadline: datetime | None = None

    contact_methods: list[ContactMethod] | None = None


class EmployerAdvertisementCreate(EmployerAdvertisementInput):
    """Input for creating an incomplete advertisement draft."""


class EmployerAdvertisementUpdate(EmployerAdvertisementInput):
    """Input for updating an employer-owned draft or published advertisement."""


class EmployerAdvertisementPositionsUpdate(BaseModel):
    """Input for updating the number of filled positions."""

    model_config = ConfigDict(extra="forbid")

    positions_filled: int = Field(ge=0)


class EmployerJobAdvertisementRead(BaseModel):
    """Private management representation of an employer's advertisement."""

    model_config = ConfigDict(from_attributes=True, extra="forbid")

    id: UUID
    title: str | None
    description: str | None
    job_type: JobType | None
    work_arrangement: WorkArrangement | None

    main_category_id: UUID | None
    subcategory_id: UUID | None
    location_id: UUID | None
    skill_ids: list[UUID]

    profession_snapshot: dict | None
    subcategory_snapshot: dict | None
    skills_snapshot: list[dict]
    location_snapshot: dict | None

    compensation_type: CompensationType | None
    min_compensation: Decimal | None
    max_compensation: Decimal | None
    payment_unit: PaymentUnit | None
    currency_code: str

    workers_needed: int | None
    positions_filled: int
    remaining_positions: int | None

    start_date: date | None
    response_deadline: datetime | None

    contact_methods: list[ContactMethod]

    status: Literal[
        "draft",
        "published",
        "suspended",
        "expired",
        "removed",
        "closed",
    ]

    moderation_reason: str | None = None
    published_at: datetime | None
    expires_at: datetime | None
    closed_at: datetime | None
    created_at: datetime
    updated_at: datetime


class EmployerJobAdvertisementPage(BaseModel):
    """Paginated employer advertisement management response."""

    model_config = ConfigDict(extra="forbid")

    items: list[EmployerJobAdvertisementRead]
    page: int
    page_size: int
    total: int
    pages: int = Field(ge=0)


class EmployerPostingCreditBalance(BaseModel):
    """Current posting-credit balance and configured package information."""

    model_config = ConfigDict(extra="forbid")

    available_credits: int
    initial_package_credits: int
    initial_package_price: Decimal
    additional_credit_price: Decimal
    currency_code: str
