"""Show or change job advertisement prices, recording every change in history.

Show current:
    python -m app.cli.set_job_ad_prices

Change:
    python -m app.cli.set_job_ad_prices --initial-price 50 --additional-price 60

Preview only:
    python -m app.cli.set_job_ad_prices --initial-price 75 --dry-run

Exits:
    0 on success
    2 when a value is rejected, settings are missing, or a database operation fails
"""

import argparse
import sys
from decimal import Decimal

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.database.session import SessionLocal
from app.models.job_advertisement import JobAdSettings
from app.services.job_ad_settings import (
    JobAdSettingsError,
    update_job_ad_settings,
)


def _parse(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Show or change job advertisement prices.")
    parser.add_argument(
        "--initial-price",
        type=int,
        help="Initial package price in whole KSh.",
    )
    parser.add_argument(
        "--initial-credits",
        type=int,
        help="Credits in the initial package.",
    )
    parser.add_argument(
        "--additional-price",
        type=int,
        help="Price per additional credit in KSh.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate and report proposed changes without writing them.",
    )

    return parser.parse_args(argv)


def _show(db: Session) -> bool:
    """Display current settings. Return False if they are not initialized."""
    settings = db.get(JobAdSettings, 1)

    if settings is None:
        print(
            "Job advertisement settings have not been initialized.",
            file=sys.stderr,
        )
        return False

    print(f"initial_package_price:   {settings.initial_package_price}")
    print(f"initial_package_credits: {settings.initial_package_credits}")
    print(f"additional_credit_price: {settings.additional_credit_price}")
    print(f"currency_code:           {settings.currency_code}")

    return True


def main(argv: list[str] | None = None) -> int:
    args = _parse(argv)

    requested = (
        args.initial_price,
        args.initial_credits,
        args.additional_price,
    )

    try:
        with SessionLocal() as db:
            if all(value is None for value in requested):
                return 0 if _show(db) else 2

            try:
                changes = update_job_ad_settings(
                    db,
                    source="cli",
                    initial_package_price=args.initial_price,
                    initial_package_credits=args.initial_credits,
                    additional_credit_price=args.additional_price,
                )

                # Capture values while the ORM objects are still attached
                # and before a dry-run rollback expires them.
                change_summary: list[tuple[str, Decimal, Decimal]] = [
                    (
                        change.setting_name,
                        change.old_value,
                        change.new_value,
                    )
                    for change in changes
                ]

                if args.dry_run:
                    db.rollback()
                else:
                    db.commit()

            except JobAdSettingsError as exc:
                db.rollback()
                print(f"Rejected: {exc}", file=sys.stderr)
                return 2

            except SQLAlchemyError:
                db.rollback()
                print(
                    "Database operation failed; no changes were committed.",
                    file=sys.stderr,
                )
                return 2

    except SQLAlchemyError:
        print(
            "Could not access the database. Check the database connection.",
            file=sys.stderr,
        )
        return 2

    if not change_summary:
        print("No changes: the requested values already match.")
        return 0

    if args.dry_run:
        prefix = "DRY RUN - nothing was written. Would change"
    else:
        prefix = "Changed"

    print(f"{prefix} {len(change_summary)} setting(s):")

    for setting_name, old_value, new_value in change_summary:
        print(f"  {setting_name}: {old_value} -> {new_value}")

    print("Existing payments keep the amount they were created with.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
