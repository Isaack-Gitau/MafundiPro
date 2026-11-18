from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Index,
    Integer,
    String,
    Uuid,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base


class LoginRateLimit(Base):
    """Track hashed login rate-limit keys and their attempt windows."""

    __tablename__ = "login_rate_limits"
    __table_args__ = (
        CheckConstraint(
            "scope IN ('username_ip', 'ip')",
            name="ck_login_rate_limits_scope",
        ),
        CheckConstraint(
            "attempts >= 0",
            name="ck_login_rate_limits_attempts",
        ),
        Index(
            "uq_login_rate_limits_scope_key",
            "scope",
            "key_hash",
            unique=True,
        ),
        Index(
            "ix_login_rate_limits_window_started",
            "window_started_at",
        ),
    )

    id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        default=uuid4,
    )
    scope: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
    )
    key_hash: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
    )
    window_started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )
    attempts: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default=text("0"),
    )
    blocked_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
