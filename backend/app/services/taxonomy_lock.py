from sqlalchemy import func, select
from sqlalchemy.orm import Session

# Stable application-specific PostgreSQL advisory-lock key.
TAXONOMY_ADVISORY_LOCK_KEY = 0x4D4146554E444950


def lock_taxonomy_mutations(db: Session) -> None:
    """Serialize taxonomy changes and worker profession/skill selections."""
    db.execute(select(func.pg_advisory_xact_lock(TAXONOMY_ADVISORY_LOCK_KEY)))
