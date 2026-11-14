"""Database connection helpers.

The API uses a shared connection pool; batch jobs (ingest, embed, bench) open
short-lived connections with `connect()` because they run as one-off processes.
"""

from __future__ import annotations

import psycopg
from psycopg_pool import ConnectionPool

from complaint_warehouse.config import get_settings

_pool: ConnectionPool | None = None


def connect(autocommit: bool = False) -> psycopg.Connection:
    return psycopg.connect(get_settings().database_url, autocommit=autocommit)


def get_pool() -> ConnectionPool:
    global _pool
    if _pool is None:
        settings = get_settings()
        _pool = ConnectionPool(
            conninfo=settings.database_url,
            min_size=settings.db_pool_min_size,
            max_size=settings.db_pool_max_size,
            open=True,
        )
    return _pool


def close_pool() -> None:
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None
