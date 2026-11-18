from fastapi import APIRouter
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from app.core.errors import AppError
from app.database.session import engine

router = APIRouter(tags=["Health"])


@router.get("/health/live")
def liveness_check() -> dict[str, str]:
    """Confirm that the application process is running."""
    return {
        "status": "alive",
    }


@router.get("/health/ready")
def readiness_check() -> dict[str, str]:
    """Confirm that the application can reach its database."""
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    except SQLAlchemyError as exc:
        raise AppError(
            status_code=503,
            code="SERVICE_UNAVAILABLE",
            message="The application database is unavailable.",
        ) from exc

    return {
        "status": "ready",
        "database": "connected",
    }
