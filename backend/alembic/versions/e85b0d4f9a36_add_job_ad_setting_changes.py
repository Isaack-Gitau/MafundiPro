"""Add job advertisement setting change history

Revision ID: e85b0d4f9a36
Revises: d74fab3c8e25
Create Date: 2026-11-23 03:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "e85b0d4f9a36"
down_revision: str | Sequence[str] | None = "d74fab3c8e25"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "job_ad_setting_changes",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("setting_name", sa.String(length=40), nullable=False),
        sa.Column("old_value", sa.Numeric(12, 2), nullable=False),
        sa.Column("new_value", sa.Numeric(12, 2), nullable=False),
        sa.Column("changed_by_user_id", sa.Uuid(), nullable=True),
        sa.Column("source", sa.String(length=20), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "setting_name IN ('initial_package_price', 'initial_package_credits', "
            "'additional_credit_price')",
            name="ck_job_ad_setting_changes_name",
        ),
        sa.CheckConstraint(
            "source IN ('cli', 'admin')",
            name="ck_job_ad_setting_changes_source",
        ),
        sa.ForeignKeyConstraint(["changed_by_user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_job_ad_setting_changes_created", "job_ad_setting_changes", ["created_at"])


def downgrade() -> None:
    bind = op.get_bind()
    rows = bind.execute(sa.text("SELECT count(*) FROM job_ad_setting_changes")).scalar_one()
    if rows:
        raise RuntimeError("Cannot downgrade: price change history exists. Preserve it first.")

    op.drop_index("ix_job_ad_setting_changes_created", table_name="job_ad_setting_changes")
    op.drop_table("job_ad_setting_changes")

