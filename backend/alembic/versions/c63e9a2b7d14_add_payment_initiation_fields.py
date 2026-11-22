"""Add payment initiation fields and purchase uniqueness

Revision ID: c63e9a2b7d14
Revises: b52d8f1a6c03
Create Date: 2026-11-23 01:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "c63e9a2b7d14"
down_revision: str | Sequence[str] | None = "b52d8f1a6c03"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("payments", sa.Column("payer_phone", sa.String(length=16), nullable=True))
    op.add_column("payments", sa.Column("credits_to_grant", sa.Integer(), nullable=True))
    op.create_check_constraint(
        "ck_payments_credits_to_grant",
        "payments",
        "credits_to_grant IS NULL OR credits_to_grant > 0",
    )
    op.create_index(
        "uq_payments_one_pending_per_purpose",
        "payments",
        ["employer_profile_id", "purpose"],
        unique=True,
        postgresql_where=sa.text("status = 'pending'"),
    )
    op.create_index(
        "uq_payments_one_initial_package",
        "payments",
        ["employer_profile_id"],
        unique=True,
        postgresql_where=sa.text(
            "purpose = 'initial_employer_package' AND status IN ('pending', 'succeeded')"
        ),
    )


def downgrade() -> None:
    op.drop_index("uq_payments_one_initial_package", table_name="payments")
    op.drop_index("uq_payments_one_pending_per_purpose", table_name="payments")
    op.drop_constraint("ck_payments_credits_to_grant", "payments", type_="check")
    op.drop_column("payments", "credits_to_grant")
    op.drop_column("payments", "payer_phone")
