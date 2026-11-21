"""Allow system-initiated expiry audit entries

Revision ID: a41c7e2d9b05
Revises: eaec0bce4930
Create Date: 2026-11-21 21:30:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a41c7e2d9b05"
down_revision: str | Sequence[str] | None = "eaec0bce4930"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "job_advertisement_audits"

NEW_ACTIONS = """
action IN (
    'employer_edit', 'employer_close', 'publish', 'expire',
    'suspend', 'reinstate', 'remove'
)
"""

OLD_ACTIONS = """
action IN (
    'employer_edit', 'employer_close', 'publish',
    'suspend', 'reinstate', 'remove'
)
"""


def upgrade() -> None:
    """Allow a null actor only for the system-initiated 'expire' action."""
    op.drop_constraint("ck_job_advertisement_audits_action", TABLE, type_="check")
    op.create_check_constraint("ck_job_advertisement_audits_action", TABLE, NEW_ACTIONS)

    op.alter_column(TABLE, "actor_user_id", existing_type=sa.Uuid(), nullable=True)

    op.create_check_constraint(
        "ck_job_advertisement_audits_actor_required",
        TABLE,
        "actor_user_id IS NOT NULL OR action = 'expire'",
    )


def downgrade() -> None:
    """Restore the human-actor requirement, refusing to destroy audit history."""
    bind = op.get_bind()
    system_rows = bind.execute(
        sa.text(f"SELECT count(*) FROM {TABLE} WHERE action = 'expire'")
    ).scalar_one()
    if system_rows:
        raise RuntimeError(
            f"Cannot downgrade: {system_rows} system 'expire' audit rows exist. "
            "Preserve or archive them before downgrading."
        )

    op.drop_constraint("ck_job_advertisement_audits_actor_required", TABLE, type_="check")
    op.alter_column(TABLE, "actor_user_id", existing_type=sa.Uuid(), nullable=False)

    op.drop_constraint("ck_job_advertisement_audits_action", TABLE, type_="check")
    op.create_check_constraint("ck_job_advertisement_audits_action", TABLE, OLD_ACTIONS)
