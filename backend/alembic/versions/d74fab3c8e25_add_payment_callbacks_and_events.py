"""Add payment receipts, callback timestamps and payment events

Revision ID: d74fab3c8e25
Revises: c63e9a2b7d14
Create Date: 2026-11-23 02:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "d74fab3c8e25"
down_revision: str | Sequence[str] | None = "c63e9a2b7d14"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "payments", sa.Column("mpesa_receipt_number", sa.String(length=40), nullable=True)
    )
    op.add_column(
        "payments", sa.Column("callback_received_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.create_unique_constraint(
        "payments_mpesa_receipt_number_key", "payments", ["mpesa_receipt_number"]
    )
    op.create_table(
        "payment_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("payment_id", sa.Uuid(), nullable=True),
        sa.Column("provider", sa.String(length=40), nullable=False),
        sa.Column("event_type", sa.String(length=20), nullable=False),
        sa.Column("outcome", sa.String(length=40), nullable=False),
        sa.Column(
            "payload",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "event_type IN ('callback', 'status_query')",
            name="ck_payment_events_type",
        ),
        sa.ForeignKeyConstraint(["payment_id"], ["payments.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_payment_events_payment_created", "payment_events", ["payment_id", "created_at"]
    )


def downgrade() -> None:
    bind = op.get_bind()
    events = bind.execute(sa.text("SELECT count(*) FROM payment_events")).scalar_one()
    receipts = bind.execute(
        sa.text("SELECT count(*) FROM payments WHERE mpesa_receipt_number IS NOT NULL")
    ).scalar_one()
    if events or receipts:
        raise RuntimeError(
            "Cannot downgrade: payment events or receipts exist. Preserve them first."
        )

    op.drop_index("ix_payment_events_payment_created", table_name="payment_events")
    op.drop_table("payment_events")
    op.drop_constraint("payments_mpesa_receipt_number_key", "payments", type_="unique")
    op.drop_column("payments", "callback_received_at")
    op.drop_column("payments", "mpesa_receipt_number")
