"""FastAPI application. Search endpoints are added in phase 5; for now it exposes health only."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from complaint_warehouse import __version__
from complaint_warehouse.db import close_pool, get_pool


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    yield
    close_pool()


app = FastAPI(
    title="complaint-warehouse",
    version=__version__,
    description="Hybrid (full-text + semantic) search over CFPB consumer complaints.",
    lifespan=lifespan,
)


def check_database() -> dict[str, str]:
    """Return Postgres and pgvector versions; raises if the database is unreachable."""
    with get_pool().connection(timeout=2) as conn:
        pg_version = conn.execute("SHOW server_version").fetchone()[0]
        row = conn.execute(
            "SELECT extversion FROM pg_extension WHERE extname = 'vector'"
        ).fetchone()
    return {"postgres": pg_version, "pgvector": row[0] if row else "missing"}


@app.get("/health")
def health() -> JSONResponse:
    try:
        db = check_database()
    except Exception as exc:  # health must report, not raise
        return JSONResponse(
            status_code=503,
            content={"status": "degraded", "version": __version__, "error": type(exc).__name__},
        )
    return JSONResponse(content={"status": "ok", "version": __version__, "database": db})
