"""Global safeguards for the MafundiPro test suite."""

import os
from importlib import import_module

# Set the isolated database before importing application configuration
# or constructing the global SQLAlchemy engine.
os.environ["POSTGRES_DB"] = "mafundipro_test"

settings = import_module("app.core.config").settings
engine = import_module("app.database.session").engine

EXPECTED_TEST_DATABASE = "mafundipro_test"


def pytest_configure() -> None:
    """Fail closed unless the test process uses the isolated test database."""
    configured_database = settings.database_url.database
    engine_database = engine.url.database

    if configured_database != EXPECTED_TEST_DATABASE:
        raise RuntimeError(
            "Unsafe test database configuration: "
            f"expected {EXPECTED_TEST_DATABASE!r}, "
            f"got {configured_database!r}."
        )

    if engine_database != EXPECTED_TEST_DATABASE:
        raise RuntimeError(
            "Unsafe global SQLAlchemy engine: "
            f"expected {EXPECTED_TEST_DATABASE!r}, "
            f"got {engine_database!r}."
        )

    with engine.connect() as connection:
        actual_database = connection.exec_driver_sql("SELECT current_database()").scalar_one()

    if actual_database != EXPECTED_TEST_DATABASE:
        raise RuntimeError(
            "Unsafe PostgreSQL connection: "
            f"expected {EXPECTED_TEST_DATABASE!r}, "
            f"connected to {actual_database!r}."
        )
