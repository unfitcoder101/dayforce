"""Connections and schema setup. Settings come only from env vars (DATABASE_URL)."""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

from app.ai.retrieval import to_psycopg_dsn

SCHEMA_PATH = Path(__file__).with_name("schema.sql")

log = logging.getLogger("app.db")


class DatabaseNotConfigured(RuntimeError):
    """DATABASE_URL is not set."""


def database_dsn() -> str:
    """DATABASE_URL from env. SQLAlchemy-style 'postgresql+psycopg://' URLs are accepted."""
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise DatabaseNotConfigured("DATABASE_URL is not set")
    return to_psycopg_dsn(url)


def connect() -> psycopg.Connection:
    return psycopg.connect(database_dsn(), row_factory=dict_row, connect_timeout=5)


def init_schema(conn: psycopg.Connection) -> None:
    """Create extension, tables and the audit trigger if missing (idempotent).

    Several app copies may start at once (Kubernetes), so a transaction-level advisory lock
    makes them run the schema one after another instead of racing.
    """
    with conn.transaction():
        conn.execute("SELECT pg_advisory_xact_lock(727001)")
        conn.execute(SCHEMA_PATH.read_text())  # no parameters, so several statements are fine
    conn.commit()


def init_schema_with_retry(
    timeout_s: float,
    interval_s: float = 2.0,
    *,
    connect_fn: Callable[[], psycopg.Connection] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> None:
    """Run init_schema, trying again every `interval_s` seconds for up to `timeout_s` seconds.

    On a fresh deployment the database can still be starting when the API starts. With
    timeout_s=0 this tries once (the old behaviour). A missing DATABASE_URL is never retried.
    Raises the last error if the database is still not ready when the time is up.
    """
    connect_fn = connect_fn or connect
    deadline = clock() + max(timeout_s, 0.0)
    attempt = 0
    while True:
        attempt += 1
        try:
            with connect_fn() as conn:
                init_schema(conn)
            return
        except DatabaseNotConfigured:
            raise
        except Exception as e:
            if clock() + interval_s > deadline:
                raise
            log.info(
                "database not ready (attempt %d, %s); trying again in %.0fs",
                attempt,
                type(e).__name__,
                interval_s,
            )
            sleep(interval_s)


def get_conn() -> Iterator[psycopg.Connection]:
    """FastAPI dependency: one connection and ONE transaction per request.

    Committed when the handler returns (before the response is sent, see `Conn` in api/routes.py),
    rolled back if it raises, so a decision and the
    timecard change it causes are saved together or not at all. Nothing is kept in memory
    between requests, so any number of app copies can run side by side.
    """
    conn = connect()
    try:
        yield conn
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()
