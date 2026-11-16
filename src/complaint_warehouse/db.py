"""Database connection helpers.

The API uses a shared connection pool; batch jobs (ingest, embed, bench) open
short-lived connections with `connect()` because they run as one-off processes.
"""

from __future__ import annotations

import logging

import psycopg
from psycopg_pool import ConnectionPool

from complaint_warehouse.config import get_settings

log = logging.getLogger(__name__)

_pool: ConnectionPool | None = None


def _configure(conn: psycopg.Connection) -> None:
    """Register the pgvector type once per pooled connection, not once per request."""
    from pgvector.psycopg import register_vector

    try:
        register_vector(conn)
    except psycopg.ProgrammingError:
        # Extension missing: /health still works and reports it; search will fail loudly.
        log.warning("pgvector extension not found; vector parameters will not adapt")
    conn.commit()


def connect(autocommit: bool = False) -> psycopg.Connection:
    return psycopg.connect(get_settings().database_url, autocommit=autocommit)


def get_pool() -> ConnectionPool:
    global _pool
    if _pool is None:
        settings = get_settings()
        # A statement timeout caps what one API request can cost the database.
        options = f"-c statement_timeout={settings.search_statement_timeout_ms}"
        _pool = ConnectionPool(
            conninfo=settings.database_url,
            min_size=settings.db_pool_min_size,
            max_size=settings.db_pool_max_size,
            kwargs={"options": options},
            configure=_configure,
            open=True,
        )
    return _pool


def close_pool() -> None:
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None
