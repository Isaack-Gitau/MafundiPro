from sqlalchemy import func, select
from sqlalchemy.orm import Session

LOCATION_ADVISORY_LOCK_KEY = 0x4D4146554E44494C


def lock_location_mutations(db: Session) -> None:
    """Serialize location hierarchy mutations within PostgreSQL."""
    db.execute(select(func.pg_advisory_xact_lock(LOCATION_ADVISORY_LOCK_KEY)))
