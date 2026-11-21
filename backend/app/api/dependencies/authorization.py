from typing import Annotated

from fastapi import Depends
from sqlalchemy import select

from app.api.dependencies.authentication import CurrentUser
from app.api.dependencies.database import DatabaseSession
from app.core.errors import AppError
from app.models.identity import User, UserRole


def require_admin(
    current_user: CurrentUser,
    db: DatabaseSession,
) -> User:
    """Require the authenticated user to have the administrator role."""
    is_admin = db.scalar(
        select(UserRole.id)
        .where(
            UserRole.user_id == current_user.id,
            UserRole.role == "admin",
        )
        .limit(1)
    )

    if is_admin is None:
        raise AppError(
            status_code=403,
            code="ADMIN_REQUIRED",
            message="Administrator privileges are required.",
        )

    return current_user


def require_moderator_or_admin(
    current_user: CurrentUser,
    db: DatabaseSession,
) -> User:
    """Require moderator or administrator privileges."""
    has_permission = db.scalar(
        select(UserRole.id)
        .where(
            UserRole.user_id == current_user.id,
            UserRole.role.in_(("moderator", "admin")),
        )
        .limit(1)
    )

    if has_permission is None:
        raise AppError(
            status_code=403,
            code="MODERATOR_OR_ADMIN_REQUIRED",
            message="Moderator or administrator privileges are required.",
        )

    return current_user


AdminUser = Annotated[User, Depends(require_admin)]
ModeratorOrAdmin = Annotated[User, Depends(require_moderator_or_admin)]
