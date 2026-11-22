"""Add FIFO credit sources and payment refunds

Revision ID: b52d8f1a6c03
Revises: a41c7e2d9b05
Create Date: 2026-11-23 00:30:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "b52d8f1a6c03"
down_revision: str | Sequence[str] | None = "a41c7e2d9b05"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

LEDGER = "employer_posting_credit_entries"

NEW_TYPES = (
    "entry_type IN ('initial_grant', 'additional_purchase', 'consumption', 'refund_reversal')"
)
OLD_TYPES = "entry_type IN ('initial_grant', 'additional_purchase', 'consumption')"

NEW_INTEGRITY = """
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
"""

OLD_INTEGRITY = """
(
    entry_type IN ('initial_grant', 'additional_purchase')
    AND credit_delta > 0
    AND payment_id IS NOT NULL
    AND advertisement_id IS NULL
)
OR (
    entry_type = 'consumption'
    AND credit_delta = -1
    AND payment_id IS NULL
    AND advertisement_id IS NOT NULL
)
"""


def upgrade() -> None:
    op.create_table(
        "payment_refunds",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("payment_id", sa.Uuid(), nullable=False),
        sa.Column("employer_profile_id", sa.Uuid(), nullable=False),
        sa.Column("credits_refunded", sa.Integer(), nullable=False),
        sa.Column("amount", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column("currency_code", sa.String(length=3), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("refunded_by_user_id", sa.Uuid(), nullable=False),
        sa.Column(
            "payout_status",
            sa.String(length=20),
            server_default=sa.text("'recorded'"),
            nullable=False,
        ),
        sa.Column("payout_reference", sa.String(length=160), nullable=True),
        sa.Column("paid_out_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "payout_status IN ('recorded', 'paid_out')",
            name="ck_payment_refunds_payout_status",
        ),
        sa.CheckConstraint("credits_refunded > 0", name="ck_payment_refunds_credits"),
        sa.CheckConstraint("amount > 0", name="ck_payment_refunds_amount"),
        sa.CheckConstraint("length(currency_code) = 3", name="ck_payment_refunds_currency"),
        sa.CheckConstraint("length(trim(reason)) > 0", name="ck_payment_refunds_reason"),
        sa.CheckConstraint(
            "payout_status = 'recorded' OR paid_out_at IS NOT NULL",
            name="ck_payment_refunds_paid_out_at",
        ),
        sa.ForeignKeyConstraint(["payment_id"], ["payments.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["employer_profile_id"], ["employer_profiles.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["refunded_by_user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("payment_id"),
    )
    op.create_index("ix_payment_refunds_employer", "payment_refunds", ["employer_profile_id"])

    op.add_column(LEDGER, sa.Column("source_payment_id", sa.Uuid(), nullable=True))
    op.add_column(LEDGER, sa.Column("refund_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        "fk_posting_credit_source_payment",
        LEDGER,
        "payments",
        ["source_payment_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_posting_credit_refund",
        LEDGER,
        "payment_refunds",
        ["refund_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index(
        "uq_employer_posting_credit_refund",
        LEDGER,
        ["refund_id"],
        unique=True,
        postgresql_where=sa.text("refund_id IS NOT NULL"),
    )
    op.create_index("ix_employer_posting_credit_source_payment", LEDGER, ["source_payment_id"])

    op.drop_constraint("ck_employer_posting_credit_entries_type", LEDGER, type_="check")
    op.drop_constraint("ck_employer_posting_credit_entries_integrity", LEDGER, type_="check")
    op.create_check_constraint("ck_employer_posting_credit_entries_type", LEDGER, NEW_TYPES)
    op.create_check_constraint(
        "ck_employer_posting_credit_entries_integrity", LEDGER, NEW_INTEGRITY
    )


def downgrade() -> None:
    bind = op.get_bind()
    blocking = bind.execute(
        sa.text(f"SELECT count(*) FROM {LEDGER} WHERE entry_type = 'refund_reversal'")
    ).scalar_one()
    refunds = bind.execute(sa.text("SELECT count(*) FROM payment_refunds")).scalar_one()
    if blocking or refunds:
        raise RuntimeError(
            "Cannot downgrade: refund records exist. Preserve or archive them first."
        )

    op.drop_constraint("ck_employer_posting_credit_entries_integrity", LEDGER, type_="check")
    op.drop_constraint("ck_employer_posting_credit_entries_type", LEDGER, type_="check")
    op.drop_index("ix_employer_posting_credit_source_payment", table_name=LEDGER)
    op.drop_index("uq_employer_posting_credit_refund", table_name=LEDGER)
    op.drop_constraint("fk_posting_credit_refund", LEDGER, type_="foreignkey")
    op.drop_constraint("fk_posting_credit_source_payment", LEDGER, type_="foreignkey")
    op.drop_column(LEDGER, "refund_id")
    op.drop_column(LEDGER, "source_payment_id")
    op.create_check_constraint("ck_employer_posting_credit_entries_type", LEDGER, OLD_TYPES)
    op.create_check_constraint(
        "ck_employer_posting_credit_entries_integrity", LEDGER, OLD_INTEGRITY
    )
    op.drop_index("ix_payment_refunds_employer", table_name="payment_refunds")
    op.drop_table("payment_refunds")
