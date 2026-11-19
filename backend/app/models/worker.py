from datetime import datetime
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database.base import Base

if TYPE_CHECKING:
    from app.models.identity import User
    from app.models.taxonomy import Category


class WorkerProfile(Base):
    """Represent the worker-specific profile belonging to a user account."""

    __tablename__ = "worker_profiles"
    __table_args__ = (
        UniqueConstraint("user_id", name="uq_worker_profiles_user_id"),
        CheckConstraint(
            "experience_years IS NULL OR experience_years >= 0",
            name="ck_worker_profiles_experience_years",
        ),
        CheckConstraint(
            "availability IN ('available', 'busy', 'unavailable')",
            name="ck_worker_profiles_availability",
        ),
    )

    id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        default=uuid4,
    )
    user_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    display_name: Mapped[str] = mapped_column(
        String(120),
        nullable=False,
    )
    bio: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )
    service_description: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )
    experience_years: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
    )
    availability: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default="unavailable",
        server_default=text("'unavailable'"),
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

    user: Mapped["User"] = relationship()
    professions: Mapped[list["WorkerProfession"]] = relationship(
        back_populates="worker_profile",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


class WorkerProfession(Base):
    """Represent one root profession selected by a worker."""

    __tablename__ = "worker_professions"
    __table_args__ = (
        UniqueConstraint(
            "worker_profile_id",
            "category_id",
            name="uq_worker_professions_profile_category",
        ),
        Index(
            "ix_worker_professions_category_id",
            "category_id",
        ),
    )

    id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        default=uuid4,
    )
    worker_profile_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("worker_profiles.id", ondelete="CASCADE"),
        nullable=False,
    )
    category_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("categories.id", ondelete="RESTRICT"),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    worker_profile: Mapped["WorkerProfile"] = relationship(
        back_populates="professions",
    )
    category: Mapped["Category"] = relationship()
    skills: Mapped[list["WorkerSkill"]] = relationship(
        back_populates="profession",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


class WorkerSkill(Base):
    """Represent a skill selected under one of a worker's professions."""

    __tablename__ = "worker_skills"
    __table_args__ = (
        ForeignKeyConstraint(
            ["worker_profile_id", "main_category_id"],
            [
                "worker_professions.worker_profile_id",
                "worker_professions.category_id",
            ],
            name="fk_worker_skills_worker_profession",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["category_id", "skill_id"],
            [
                "category_skills.category_id",
                "category_skills.skill_id",
            ],
            name="fk_worker_skills_category_skill",
            ondelete="RESTRICT",
        ),
        UniqueConstraint(
            "worker_profile_id",
            "main_category_id",
            "skill_id",
            name="uq_worker_skills_profile_profession_skill",
        ),
        Index(
            "ix_worker_skills_category_skill",
            "category_id",
            "skill_id",
        ),
    )

    id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        default=uuid4,
    )
    worker_profile_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        nullable=False,
    )
    main_category_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        nullable=False,
    )
    category_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        nullable=False,
    )
    skill_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    profession: Mapped["WorkerProfession"] = relationship(
        back_populates="skills",
    )
