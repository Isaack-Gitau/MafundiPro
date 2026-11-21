from datetime import date, datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

JobType = Literal[
    "casual_daily",
    "temporary",
    "contract_project",
    "part_time",
    "full_time_permanent",
]

WorkArrangement = Literal["on_site", "remote", "hybrid"]
CompensationType = Literal["fixed", "negotiable", "undisclosed"]
PaymentUnit = Literal["hourly", "daily", "weekly", "monthly", "fixed"]
ContactMethod = Literal["phone_call", "whatsapp", "in_app_message"]
EmployerType = Literal["individual", "business", "organization", "contractor"]


class PublicAdvertisementSchema(BaseModel):
    """Base configuration for public advertisement responses."""

    model_config = ConfigDict(from_attributes=True, extra="forbid")


class PublicProfessionSnapshot(PublicAdvertisementSchema):
    id: UUID
    name: str
    path: list[str]


class PublicSubcategorySnapshot(PublicAdvertisementSchema):
    id: UUID
    name: str
    path: list[str]


class PublicSkillSnapshot(PublicAdvertisementSchema):
    id: UUID
    name: str


class PublicLocationPathEntry(PublicAdvertisementSchema):
    id: UUID
    name: str
    type: str


class PublicLocationSnapshot(PublicAdvertisementSchema):
    id: UUID
    name: str
    path: list[PublicLocationPathEntry]


class PublicEmployerSummary(PublicAdvertisementSchema):
    display_name: str
    employer_type: EmployerType


class PublicJobAdvertisementRead(PublicAdvertisementSchema):
    id: UUID
    title: str
    description: str
    job_type: JobType
    work_arrangement: WorkArrangement

    profession: PublicProfessionSnapshot
    subcategory: PublicSubcategorySnapshot | None
    required_skills: list[PublicSkillSnapshot]
    location: PublicLocationSnapshot | None

    compensation_type: CompensationType
    min_compensation: Decimal | None
    max_compensation: Decimal | None
    payment_unit: PaymentUnit | None
    currency_code: str | None

    workers_needed: int
    positions_filled: int
    remaining_positions: int

    start_date: date | None
    response_deadline: datetime | None
    published_at: datetime
    expires_at: datetime

    employer: PublicEmployerSummary
    available_contact_methods: list[ContactMethod]


class PublicJobAdvertisementPage(PublicAdvertisementSchema):
    items: list[PublicJobAdvertisementRead]
    page: int
    page_size: int
    total: int
    pages: int = Field(ge=0)
