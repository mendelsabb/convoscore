"""Run database migrations. Entry point for the API pod's init container.

Two things make this safe to run on every pod start:

* it waits for PostgreSQL instead of exiting non-zero, so a slow database shows up as a waiting
  pod with clear logs rather than a crash loop;
* it holds a PostgreSQL advisory lock, so concurrent replicas cannot run Alembic at the same time.
"""

from __future__ import annotations

import sys
from pathlib import Path

from alembic.config import Config
from sqlalchemy import text

from alembic import command
from app.config import get_settings
from app.db import create_db_engine, wait_for_database
from app.logging import configure_logging, get_logger

log = get_logger(__name__)

# Any stable 64-bit constant; it only has to be the same in every replica.
MIGRATION_LOCK_ID = 8_233_517_001


def alembic_config(database_url: str) -> Config:
    root = Path(__file__).resolve().parent.parent
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "alembic"))
    config.set_main_option("sqlalchemy.url", database_url)
    return config


def run_migrations() -> None:
    settings = get_settings()
    configure_logging("migrate", settings.log_level, settings.log_json)

    # statement_timeout=0: a migration is allowed to take as long as it takes. Being killed by the
    # request-path timeout half way through a schema change is far worse than waiting.
    engine = create_db_engine(settings, statement_timeout_ms=0)
    wait_for_database(engine)

    with engine.connect() as connection:
        log.info("migration_lock_waiting")
        connection.execute(
            text("SELECT pg_advisory_lock(:lock_id)"), {"lock_id": MIGRATION_LOCK_ID}
        )
        try:
            log.info("migration_started")
            config = alembic_config(settings.sqlalchemy_url)
            config.attributes["connection"] = connection
            command.upgrade(config, "head")
            connection.commit()
            log.info("migration_completed")
        finally:
            connection.execute(
                text("SELECT pg_advisory_unlock(:lock_id)"), {"lock_id": MIGRATION_LOCK_ID}
            )
    engine.dispose()


def main() -> int:
    try:
        run_migrations()
    except Exception:
        log.exception("migration_failed")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
