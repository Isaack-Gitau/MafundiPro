from datetime import datetime
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    Uuid,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database.base import Base


class Location(Base):
    """Represent a reusable, provider-independent hierarchical location."""

    __tablename__ = "locations"
    __table_args__ = (
        CheckConstraint(
            """
            (
                location_type = 'country'
                AND parent_id IS NULL
                AND country_code IS NOT NULL
            )
            OR
            (
                location_type <> 'country'
                AND parent_id IS NOT NULL
                AND country_code IS NULL
            )
            """,
            name="ck_locations_country_hierarchy",
        ),
        CheckConstraint(
            """
            country_code IS NULL
            OR country_code ~ '^[A-Z]{2}$'
            """,
            name="ck_locations_country_code",
        ),
        CheckConstraint(
            """
            (latitude IS NULL AND longitude IS NULL)
            OR
            (
                latitude IS NOT NULL
                AND longitude IS NOT NULL
                AND latitude BETWEEN -90 AND 90
                AND longitude BETWEEN -180 AND 180
            )
            """,
            name="ck_locations_coordinates",
        ),
        CheckConstraint(
            "length(trim(location_type)) > 0",
            name="ck_locations_location_type_not_empty",
        ),
        CheckConstraint(
            "display_order >= 0",
            name="ck_locations_display_order_non_negative",
        ),
    )

    id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        default=uuid4,
    )
    parent_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("locations.id", ondelete="RESTRICT"),
        nullable=True,
        index=True,
    )
    location_type: Mapped[str] = mapped_column(
        String(40),
        nullable=False,
    )
    name: Mapped[str] = mapped_column(
        String(120),
        nullable=False,
    )
    name_key: Mapped[str] = mapped_column(
        String(120),
        nullable=False,
    )
    country_code: Mapped[str | None] = mapped_column(
        String(2),
        nullable=True,
    )
    description: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )
    latitude: Mapped[Decimal | None] = mapped_column(
        Numeric(9, 6),
        nullable=True,
    )
    longitude: Mapped[Decimal | None] = mapped_column(
        Numeric(10, 7),
        nullable=True,
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        server_default=text("true"),
    )
    display_order: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default=text("0"),
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    parent: Mapped["Location | None"] = relationship(
        "Location",
        remote_side="Location.id",
        back_populates="children",
    )
    children: Mapped[list["Location"]] = relationship(
        "Location",
        back_populates="parent",
    )


Index(
    "uq_locations_root_name_key",
    Location.name_key,
    unique=True,
    postgresql_where=text("parent_id IS NULL"),
)
Index(
    "uq_locations_sibling_name_key",
    Location.parent_id,
    Location.name_key,
    unique=True,
    postgresql_where=text("parent_id IS NOT NULL"),
)
Index(
    "uq_locations_country_code",
    Location.country_code,
    unique=True,
    postgresql_where=text("country_code IS NOT NULL"),
)
