"""Require administrator actor for job ad setting changes.

Revision ID: 2abbb8f6b7f7
Revises: e85b0d4f9a36
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "2abbb8f6b7f7"
down_revision: str | Sequence[str] | None = "e85b0d4f9a36"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Require an actor for administrator-originated changes."""
    op.create_check_constraint(
        "ck_job_ad_setting_changes_admin_actor",
        "job_ad_setting_changes",
        "source != 'admin' OR changed_by_user_id IS NOT NULL",
    )


def downgrade() -> None:
    """Preserve the constraint while audit history exists."""
    bind = op.get_bind()
    rows = bind.execute(sa.text("SELECT count(*) FROM job_ad_setting_changes")).scalar_one()

    if rows:
        raise RuntimeError("Cannot downgrade: job advertisement setting history exists.")

    op.drop_constraint(
        "ck_job_ad_setting_changes_admin_actor",
        "job_ad_setting_changes",
        type_="check",
    )
