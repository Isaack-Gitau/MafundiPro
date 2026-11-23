from datetime import date, datetime
from decimal import Decimal
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    Column,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Table,
    Text,
    Uuid,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database.base import Base

if TYPE_CHECKING:
    from app.models.employer import EmployerProfile
    from app.models.location import Location
    from app.models.taxonomy import Category, Skill


job_advertisement_skills = Table(
    "job_advertisement_skills",
    Base.metadata,
    Column(
        "advertisement_id",
        Uuid(as_uuid=True),
        ForeignKey("job_advertisements.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column(
        "skill_id",
        Uuid(as_uuid=True),
        ForeignKey("skills.id", ondelete="RESTRICT"),
        primary_key=True,
    ),
)


class JobAdvertisementContactMethod(Base):
    """Represent one contact method enabled on an advertisement."""

    __tablename__ = "job_advertisement_contact_methods"
    __table_args__ = (
        CheckConstraint(
            "contact_method IN ('phone_call', 'whatsapp', 'in_app_message')",
            name="ck_job_advertisement_contact_methods_value",
        ),
    )

    advertisement_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("job_advertisements.id", ondelete="CASCADE"),
        primary_key=True,
    )
    contact_method: Mapped[str] = mapped_column(
        String(30),
        primary_key=True,
    )

    advertisement: Mapped["JobAdvertisement"] = relationship(
        back_populates="contact_methods",
    )


class JobAdvertisement(Base):
    """Represent an employer-managed job advertisement."""

    __tablename__ = "job_advertisements"
    __table_args__ = (
        CheckConstraint(
            """
            status IN (
                'draft', 'published', 'suspended',
                'expired', 'removed', 'closed'
            )
            """,
            name="ck_job_advertisements_status",
        ),
        CheckConstraint(
            """
            job_type IS NULL OR job_type IN (
                'casual_daily', 'temporary', 'contract_project',
                'part_time', 'full_time_permanent'
            )
            """,
            name="ck_job_advertisements_job_type",
        ),
        CheckConstraint(
            """
            work_arrangement IS NULL OR work_arrangement IN (
                'on_site', 'remote', 'hybrid'
            )
            """,
            name="ck_job_advertisements_work_arrangement",
        ),
        CheckConstraint(
            """
            compensation_type IS NULL OR compensation_type IN (
                'fixed', 'negotiable', 'undisclosed'
            )
            """,
            name="ck_job_advertisements_compensation_type",
        ),
        CheckConstraint(
            """
            payment_unit IS NULL OR payment_unit IN (
                'hourly', 'daily', 'weekly', 'monthly', 'fixed'
            )
            """,
            name="ck_job_advertisements_payment_unit",
        ),
        CheckConstraint(
            "min_compensation IS NULL OR min_compensation >= 0",
            name="ck_job_advertisements_min_compensation",
        ),
        CheckConstraint(
            "max_compensation IS NULL OR max_compensation >= 0",
            name="ck_job_advertisements_max_compensation",
        ),
        CheckConstraint(
            """
            min_compensation IS NULL OR max_compensation IS NULL
            OR max_compensation >= min_compensation
            """,
            name="ck_job_advertisements_compensation_range",
        ),
        CheckConstraint(
            """
            workers_needed IS NULL OR workers_needed >= 1
            """,
            name="ck_job_advertisements_workers_needed",
        ),
        CheckConstraint(
            """
            positions_filled >= 0
            AND (
                workers_needed IS NULL
                OR positions_filled <= workers_needed
            )
            """,
            name="ck_job_advertisements_positions_filled",
        ),
        CheckConstraint(
            """
            status = 'draft' OR (
                title IS NOT NULL
                AND length(trim(title)) > 0
                AND description IS NOT NULL
                AND length(trim(description)) > 0
                AND job_type IS NOT NULL
                AND work_arrangement IS NOT NULL
                AND main_category_id IS NOT NULL
                AND compensation_type IS NOT NULL
                AND workers_needed IS NOT NULL
                AND published_at IS NOT NULL
                AND expires_at IS NOT NULL
                AND profession_snapshot IS NOT NULL
                AND skills_snapshot IS NOT NULL
                AND (
                    work_arrangement = 'remote'
                    OR location_id IS NOT NULL
                )
                AND (
                    (
                        compensation_type = 'fixed'
                        AND payment_unit IS NOT NULL
                        AND (
                            min_compensation IS NOT NULL
                            OR max_compensation IS NOT NULL
                        )
                    )
                    OR (
                        compensation_type IN ('negotiable', 'undisclosed')
                        AND min_compensation IS NULL
                        AND max_compensation IS NULL
                    )
                )
            )
            """,
            name="ck_job_advertisements_published_requirements",
        ),
        CheckConstraint(
            """
            status NOT IN ('published', 'expired', 'suspended', 'closed', 'removed')
            OR (
                published_at IS NOT NULL
                AND expires_at IS NOT NULL
                AND expires_at > published_at
            )
            """,
            name="ck_job_advertisements_publication_dates",
        ),
        CheckConstraint(
            """
            response_deadline IS NULL
            OR expires_at IS NULL
            OR response_deadline <= expires_at
            """,
            name="ck_job_advertisements_response_deadline",
        ),
        CheckConstraint(
            "length(currency_code) = 3",
            name="ck_job_advertisements_currency_code",
        ),
        Index(
            "ix_job_advertisements_publication_expiry",
            "status",
            "expires_at",
            "published_at",
        ),
        Index(
            "ix_job_advertisements_employer_status",
            "employer_profile_id",
            "status",
            "created_at",
        ),
        Index(
            "ix_job_advertisements_profession",
            "main_category_id",
            "subcategory_id",
        ),
        Index(
            "ix_job_advertisements_location",
            "location_id",
        ),
    )

    id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        default=uuid4,
    )
    employer_profile_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("employer_profiles.id", ondelete="RESTRICT"),
        nullable=False,
    )
    title: Mapped[str | None] = mapped_column(String(160), nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    job_type: Mapped[str | None] = mapped_column(String(30), nullable=True)
    work_arrangement: Mapped[str | None] = mapped_column(String(20), nullable=True)

    main_category_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("categories.id", ondelete="RESTRICT"),
        nullable=True,
    )
    subcategory_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("categories.id", ondelete="RESTRICT"),
        nullable=True,
    )
    location_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("locations.id", ondelete="RESTRICT"),
        nullable=True,
    )

    compensation_type: Mapped[str | None] = mapped_column(
        String(20),
        nullable=True,
    )
    min_compensation: Mapped[Decimal | None] = mapped_column(
        Numeric(12, 2),
        nullable=True,
    )
    max_compensation: Mapped[Decimal | None] = mapped_column(
        Numeric(12, 2),
        nullable=True,
    )
    payment_unit: Mapped[str | None] = mapped_column(String(20), nullable=True)
    currency_code: Mapped[str] = mapped_column(
        String(3),
        nullable=False,
        default="KES",
        server_default=text("'KES'"),
    )

    workers_needed: Mapped[int | None] = mapped_column(Integer, nullable=True)
    positions_filled: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default=text("0"),
    )

    start_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    response_deadline: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default="draft",
        server_default=text("'draft'"),
    )
    published_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    closed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    profession_snapshot: Mapped[dict | None] = mapped_column(
        JSONB,
        nullable=True,
    )
    subcategory_snapshot: Mapped[dict | None] = mapped_column(
        JSONB,
        nullable=True,
    )
    skills_snapshot: Mapped[list] = mapped_column(
        JSONB,
        nullable=False,
        default=list,
        server_default=text("'[]'::jsonb"),
    )
    location_snapshot: Mapped[dict | None] = mapped_column(
        JSONB,
        nullable=True,
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

    employer: Mapped["EmployerProfile"] = relationship()
    main_category: Mapped["Category | None"] = relationship(
        foreign_keys=[main_category_id],
    )
    subcategory: Mapped["Category | None"] = relationship(
        foreign_keys=[subcategory_id],
    )
    location: Mapped["Location | None"] = relationship()
    skills: Mapped[list["Skill"]] = relationship(
        secondary=job_advertisement_skills,
    )
    contact_methods: Mapped[list["JobAdvertisementContactMethod"]] = relationship(
        back_populates="advertisement",
        cascade="all, delete-orphan",
    )


class JobAdSettings(Base):
    """Store administrator-managed job advertisement and posting prices."""

    __tablename__ = "job_advertisement_settings"
    __table_args__ = (
        CheckConstraint("id = 1", name="ck_job_advertisement_settings_singleton"),
        CheckConstraint(
            "advertisement_duration_days >= 1",
            name="ck_job_advertisement_settings_duration",
        ),
        CheckConstraint(
            "initial_package_credits >= 1",
            name="ck_job_advertisement_settings_initial_credits",
        ),
        CheckConstraint(
            "initial_package_price >= 0",
            name="ck_job_advertisement_settings_initial_price",
        ),
        CheckConstraint(
            "additional_credit_price >= 0",
            name="ck_job_advertisement_settings_additional_price",
        ),
        CheckConstraint(
            "length(currency_code) = 3",
            name="ck_job_advertisement_settings_currency",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    advertisement_duration_days: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=30,
        server_default=text("30"),
    )
    initial_package_credits: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=5,
        server_default=text("5"),
    )
    initial_package_price: Mapped[Decimal] = mapped_column(
        Numeric(12, 2),
        nullable=False,
        default=Decimal("50.00"),
        server_default=text("50.00"),
    )
    additional_credit_price: Mapped[Decimal] = mapped_column(
        Numeric(12, 2),
        nullable=False,
        default=Decimal("50.00"),
        server_default=text("50.00"),
    )
    currency_code: Mapped[str] = mapped_column(
        String(3),
        nullable=False,
        default="KES",
        server_default=text("'KES'"),
    )
    updated_by_user_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
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


class Payment(Base):
    """Represent a provider payment awaiting verification or reconciliation."""

    __tablename__ = "payments"
    __table_args__ = (
        CheckConstraint(
            "purpose IN ('initial_employer_package', 'additional_posting_credit')",
            name="ck_payments_purpose",
        ),
        CheckConstraint(
            "status IN ('pending', 'succeeded', 'failed', 'cancelled')",
            name="ck_payments_status",
        ),
        CheckConstraint(
            "provider_environment IN ('sandbox', 'production')",
            name="ck_payments_environment",
        ),
        CheckConstraint("amount > 0", name="ck_payments_amount"),
        CheckConstraint(
            "length(currency_code) = 3",
            name="ck_payments_currency",
        ),
        CheckConstraint(
            "status <> 'succeeded' OR verified_at IS NOT NULL",
            name="ck_payments_success_requires_verification",
        ),
        Index(
            "uq_payments_provider_reference",
            "provider",
            "provider_reference",
            unique=True,
            postgresql_where=text("provider_reference IS NOT NULL"),
        ),
        Index("ix_payments_employer_created", "employer_profile_id", "created_at"),
        CheckConstraint(
            "credits_to_grant IS NULL OR credits_to_grant > 0",
            name="ck_payments_credits_to_grant",
        ),
        Index(
            "uq_payments_one_pending_per_purpose",
            "employer_profile_id",
            "purpose",
            unique=True,
            postgresql_where=text("status = 'pending'"),
        ),
        Index(
            "uq_payments_one_initial_package",
            "employer_profile_id",
            unique=True,
            postgresql_where=text(
                "purpose = 'initial_employer_package' AND status IN ('pending', 'succeeded')"
            ),
        ),
    )

    id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        default=uuid4,
    )
    employer_profile_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("employer_profiles.id", ondelete="RESTRICT"),
        nullable=False,
    )
    initiated_by_user_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    purpose: Mapped[str] = mapped_column(String(40), nullable=False)
    provider: Mapped[str] = mapped_column(String(40), nullable=False)
    provider_environment: Mapped[str] = mapped_column(
        String(12),
        nullable=False,
        default="sandbox",
        server_default=text("'sandbox'"),
    )
    idempotency_key: Mapped[str] = mapped_column(
        String(128),
        nullable=False,
        unique=True,
    )
    provider_reference: Mapped[str | None] = mapped_column(
        String(160),
        nullable=True,
    )
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    currency_code: Mapped[str] = mapped_column(String(3), nullable=False)
    status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default="pending",
        server_default=text("'pending'"),
    )
    provider_metadata: Mapped[dict] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
        server_default=text("'{}'::jsonb"),
    )
    payer_phone: Mapped[str | None] = mapped_column(String(16), nullable=True)
    credits_to_grant: Mapped[int | None] = mapped_column(Integer, nullable=True)
    mpesa_receipt_number: Mapped[str | None] = mapped_column(
        String(40),
        nullable=True,
        unique=True,
    )
    callback_received_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    verified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
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


class EmployerPostingCreditEntry(Base):
    """Record an immutable grant, purchase, or consumption of posting credits."""

    __tablename__ = "employer_posting_credit_entries"
    __table_args__ = (
        CheckConstraint(
            (
                "entry_type IN ('initial_grant', 'additional_purchase', "
                "'consumption', 'refund_reversal')"
            ),
            name="ck_employer_posting_credit_entries_type",
        ),
        CheckConstraint(
            """
            (
                entry_type IN ('initial_grant', 'additional_purchase')
                AND credit_delta > 0
                AND payment_id IS NOT NULL
                AND advertisement_id IS NULL
                AND source_payment_id IS NULL
                AND refund_id IS NULL
            )
            OR (
                entry_type = 'consumption'
                AND credit_delta = -1
                AND payment_id IS NULL
                AND advertisement_id IS NOT NULL
                AND source_payment_id IS NOT NULL
                AND refund_id IS NULL
            )
            OR (
                entry_type = 'refund_reversal'
                AND credit_delta < 0
                AND payment_id IS NULL
                AND advertisement_id IS NULL
                AND source_payment_id IS NOT NULL
                AND refund_id IS NOT NULL
            )
            """,
            name="ck_employer_posting_credit_entries_integrity",
        ),
        Index(
            "uq_employer_posting_credit_payment",
            "payment_id",
            unique=True,
            postgresql_where=text("payment_id IS NOT NULL"),
        ),
        Index(
            "uq_employer_posting_credit_advertisement",
            "advertisement_id",
            unique=True,
            postgresql_where=text("advertisement_id IS NOT NULL"),
        ),
        Index(
            "ix_employer_posting_credit_employer_created",
            "employer_profile_id",
            "created_at",
        ),
        Index(
            "uq_employer_posting_credit_refund",
            "refund_id",
            unique=True,
            postgresql_where=text("refund_id IS NOT NULL"),
        ),
        Index(
            "ix_employer_posting_credit_source_payment",
            "source_payment_id",
        ),
    )

    id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        default=uuid4,
    )
    employer_profile_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("employer_profiles.id", ondelete="RESTRICT"),
        nullable=False,
    )
    entry_type: Mapped[str] = mapped_column(String(30), nullable=False)
    credit_delta: Mapped[int] = mapped_column(Integer, nullable=False)
    payment_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("payments.id", ondelete="RESTRICT"),
        nullable=True,
    )
    advertisement_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("job_advertisements.id", ondelete="RESTRICT"),
        nullable=True,
    )
    source_payment_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("payments.id", ondelete="RESTRICT"),
        nullable=True,
    )
    refund_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("payment_refunds.id", ondelete="RESTRICT"),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )


class JobAdvertisementAudit(Base):
    """Preserve advertisement edits and moderation decisions."""

    __tablename__ = "job_advertisement_audits"
    __table_args__ = (
        CheckConstraint(
            """
            action IN (
                'employer_edit', 'employer_close', 'publish', 'expire',
                'suspend', 'reinstate', 'remove'
            )
            """,
            name="ck_job_advertisement_audits_action",
        ),
        CheckConstraint(
            """
            action NOT IN ('suspend', 'reinstate', 'remove')
            OR (
                reason IS NOT NULL
                AND length(trim(reason)) > 0
            )
            """,
            name="ck_job_advertisement_audits_moderation_reason",
        ),
        CheckConstraint(
            """
            actor_user_id IS NOT NULL
            OR action = 'expire'
            """,
            name="ck_job_advertisement_audits_actor_required",
        ),
        Index(
            "ix_job_advertisement_audits_advertisement_created",
            "advertisement_id",
            "created_at",
        ),
    )

    id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        default=uuid4,
    )
    advertisement_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("job_advertisements.id", ondelete="RESTRICT"),
        nullable=False,
    )
    actor_user_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=True,
    )
    action: Mapped[str] = mapped_column(String(30), nullable=False)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    changes: Mapped[dict] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
        server_default=text("'{}'::jsonb"),
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )


class PaymentRefund(Base):
    """Record an administrator refund of a payment's unconsumed posting credits."""

    __tablename__ = "payment_refunds"
    __table_args__ = (
        CheckConstraint(
            "payout_status IN ('recorded', 'paid_out')",
            name="ck_payment_refunds_payout_status",
        ),
        CheckConstraint("credits_refunded > 0", name="ck_payment_refunds_credits"),
        CheckConstraint("amount > 0", name="ck_payment_refunds_amount"),
        CheckConstraint("length(currency_code) = 3", name="ck_payment_refunds_currency"),
        CheckConstraint("length(trim(reason)) > 0", name="ck_payment_refunds_reason"),
        CheckConstraint(
            "payout_status = 'recorded' OR paid_out_at IS NOT NULL",
            name="ck_payment_refunds_paid_out_at",
        ),
        Index("ix_payment_refunds_employer", "employer_profile_id"),
    )

    id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        default=uuid4,
    )
    payment_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("payments.id", ondelete="RESTRICT"),
        nullable=False,
        unique=True,
    )
    employer_profile_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("employer_profiles.id", ondelete="RESTRICT"),
        nullable=False,
    )
    credits_refunded: Mapped[int] = mapped_column(Integer, nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    currency_code: Mapped[str] = mapped_column(String(3), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    refunded_by_user_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    payout_status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default="recorded",
        server_default=text("'recorded'"),
    )
    payout_reference: Mapped[str | None] = mapped_column(String(160), nullable=True)
    paid_out_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )


class PaymentEvent(Base):
    """Append-only audit record of a provider callback or status query."""

    __tablename__ = "payment_events"
    __table_args__ = (
        CheckConstraint(
            "event_type IN ('callback', 'status_query')",
            name="ck_payment_events_type",
        ),
        Index("ix_payment_events_payment_created", "payment_id", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        default=uuid4,
    )
    payment_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("payments.id", ondelete="RESTRICT"),
        nullable=True,
    )
    provider: Mapped[str] = mapped_column(String(40), nullable=False)
    event_type: Mapped[str] = mapped_column(String(20), nullable=False)
    outcome: Mapped[str] = mapped_column(String(40), nullable=False)
    payload: Mapped[dict] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
        server_default=text("'{}'::jsonb"),
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
