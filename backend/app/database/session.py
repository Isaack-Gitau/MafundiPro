from collections.abc import Generator

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import settings

engine = create_engine(
    settings.database_url,
    pool_pre_ping=True,
)

SessionLocal = sessionmaker(
    bind=engine,
    autoflush=False,
    expire_on_commit=False,
)


def get_db() -> Generator[Session]:
    """Provide a database session for application operations."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def get_auth_db() -> Generator[Session]:
    """Provide an independent database session for authentication operations."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
