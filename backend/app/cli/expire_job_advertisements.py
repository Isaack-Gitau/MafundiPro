"""Expire published job advertisements whose expiry time has passed.

Run on a schedule:  python -m app.cli.expire_job_advertisements
"""

import sys

from app.database.session import SessionLocal
from app.services.job_advertisements import expire_due_job_advertisements

BATCH_SIZE = 100


def main() -> int:
    total = 0

    while True:
        with SessionLocal() as db:
            expired = expire_due_job_advertisements(db, batch_size=BATCH_SIZE)
            db.commit()

        total += expired
        if expired < BATCH_SIZE:
            break

    print(f"Expired {total} job advertisement(s).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
