from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.database.session import engine
from app.services.taxonomy_lock import (
    TAXONOMY_ADVISORY_LOCK_KEY,
    lock_taxonomy_mutations,
)


def test_taxonomy_lock_is_shared_and_transaction_scoped() -> None:
    """A second transaction cannot acquire the lock until the first commits."""
    with engine.connect() as first_connection, engine.connect() as second_connection:
        first_session = Session(bind=first_connection)
        second_session = Session(bind=second_connection)

        try:
            lock_taxonomy_mutations(first_session)

            acquired_while_locked = second_session.scalar(
                select(func.pg_try_advisory_xact_lock(TAXONOMY_ADVISORY_LOCK_KEY))
            )
            assert acquired_while_locked is False

            first_session.commit()

            acquired_after_commit = second_session.scalar(
                select(func.pg_try_advisory_xact_lock(TAXONOMY_ADVISORY_LOCK_KEY))
            )
            assert acquired_after_commit is True
        finally:
            second_session.rollback()
            first_session.rollback()
            second_session.close()
            first_session.close()
