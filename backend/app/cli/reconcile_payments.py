"""Reconcile pending payments and report payment inconsistencies.

Run on a schedule:  python -m app.cli.reconcile_payments
Preview only:       python -m app.cli.reconcile_payments --dry-run

Exits 0 when everything is consistent and 1 when anything needs attention
(needs_review outcomes, processing errors or reported anomalies).
"""

import argparse
import sys
from collections import Counter
from datetime import timedelta

from app.api.dependencies.payments import get_payment_provider
from app.core.errors import AppError
from app.database.session import SessionLocal
from app.services.payment_reconciliation import (
    DEFAULT_DELAYED_AFTER,
    DEFAULT_MIN_AGE,
    find_anomalies,
    find_due_payment_ids,
    reconcile_payment,
)


def _parse(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Reconcile pending payments.")
    parser.add_argument(
        "--min-age-minutes",
        type=int,
        default=int(DEFAULT_MIN_AGE.total_seconds() // 60),
        help="Only reconcile pending payments at least this old.",
    )
    parser.add_argument("--limit", type=int, default=100, help="Maximum payments per run.")
    parser.add_argument(
        "--delayed-after-hours",
        type=int,
        default=int(DEFAULT_DELAYED_AFTER.total_seconds() // 3600),
        help="Report pending payments older than this as delayed.",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Query and report, but write nothing."
    )
    args = parser.parse_args(argv)
    if args.min_age_minutes < 0 or args.limit < 1 or args.delayed_after_hours < 1:
        parser.error("Values must be positive (min age may be 0).")
    return args


def main(argv: list[str] | None = None) -> int:
    args = _parse(argv)

    try:
        provider = get_payment_provider()
    except AppError:
        print("Payment provider is not available; nothing was reconciled.")
        return 2

    with SessionLocal() as db:
        payment_ids = find_due_payment_ids(
            db, min_age=timedelta(minutes=args.min_age_minutes), limit=args.limit
        )

    outcomes: Counter[str] = Counter()
    for payment_id in payment_ids:
        try:
            with SessionLocal() as db:
                outcome = reconcile_payment(db, provider, payment_id)
                if args.dry_run:
                    db.rollback()
                else:
                    db.commit()
        except Exception as exc:  # keep going; one bad payment must not block the rest
            print(f"Error reconciling {payment_id}: {type(exc).__name__}")
            outcome = "error"
        outcomes[outcome] += 1

    with SessionLocal() as db:
        anomalies = find_anomalies(db, delayed_after=timedelta(hours=args.delayed_after_hours))

    mode = "DRY RUN - nothing was written. " if args.dry_run else ""
    print(f"{mode}Checked {len(payment_ids)} pending payment(s).")
    for outcome, count in sorted(outcomes.items()):
        print(f"  {outcome}: {count}")

    flagged = {key: ids for key, ids in anomalies.items() if ids}
    if flagged:
        print("Anomalies:")
        for key, ids in flagged.items():
            print(f"  {key}: {len(ids)}")
            for payment_id in ids[:20]:
                print(f"    {payment_id}")

    needs_attention = outcomes["needs_review"] or outcomes["error"] or flagged
    return 1 if needs_attention else 0


if __name__ == "__main__":
    sys.exit(main())
