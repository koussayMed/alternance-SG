"""Applies the SQL migrations (app/migrations/NNN_*.sql) in order, once each.

Runs at container start, before gunicorn. A Postgres advisory lock makes replicas
that start together wait for each other.
"""

from __future__ import annotations

import logging
import os
import sys
import time
from pathlib import Path

import psycopg

from .logging_setup import configure_logging

log = logging.getLogger("app.migrate")

MIGRATIONS_DIR = Path(__file__).parent / "migrations"
ADVISORY_LOCK_ID = 727_001  # arbitrary, unique to this app


def _connect(database_url: str, attempts: int = 10, delay_s: float = 2.0) -> psycopg.Connection:
    """The database may start after the app: retry before giving up."""
    for attempt in range(1, attempts + 1):
        try:
            return psycopg.connect(database_url, autocommit=True, connect_timeout=5)
        except psycopg.OperationalError as exc:
            log.warning("database not reachable yet", extra={"attempt": attempt, "error": str(exc)})
            if attempt == attempts:
                raise
            time.sleep(delay_s)
    raise RuntimeError("unreachable")


def migrate(database_url: str) -> list[str]:
    applied_now: list[str] = []
    with _connect(database_url) as conn:
        conn.execute("SELECT pg_advisory_lock(%s)", (ADVISORY_LOCK_ID,))
        try:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations ("
                "version TEXT PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT now())"
            )
            done = {row[0] for row in conn.execute("SELECT version FROM schema_migrations")}
            for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
                if path.stem in done:
                    continue
                with conn.transaction():  # one migration = one transaction (all or nothing)
                    conn.execute(path.read_text(encoding="utf-8"))
                    conn.execute("INSERT INTO schema_migrations (version) VALUES (%s)", (path.stem,))
                applied_now.append(path.stem)
                log.info("migration applied", extra={"version": path.stem})
        finally:
            conn.execute("SELECT pg_advisory_unlock(%s)", (ADVISORY_LOCK_ID,))
    log.info("migrations up to date", extra={"applied_now": applied_now})
    return applied_now


def main() -> int:
    configure_logging(os.environ.get("LOG_LEVEL", "INFO").upper())
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        log.error("DATABASE_URL is not set")
        return 1
    migrate(database_url)
    return 0


if __name__ == "__main__":
    sys.exit(main())
