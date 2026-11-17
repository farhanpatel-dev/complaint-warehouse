"""FastAPI application: health check and hybrid search over complaint narratives."""

from __future__ import annotations

import logging
import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import date
from typing import Annotated

import psycopg
from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from complaint_warehouse import __version__
from complaint_warehouse.config import get_settings
from complaint_warehouse.db import close_pool, get_pool
from complaint_warehouse.search.service import (
    KeywordMatch,
    SearchMode,
    SearchParams,
    SearchResponse,
    SearchService,
)

log = logging.getLogger(__name__)

_service: SearchService | None = None
_service_lock = threading.Lock()


def get_search_service() -> SearchService:
    """The process-wide search service; the model is loaded once, on first use or startup."""
    global _service
    if _service is None:
        with _service_lock:
            if _service is None:
                from complaint_warehouse.embed.model import load_encoder

                _service = SearchService(get_pool(), load_encoder(get_settings()))
    return _service


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    if get_settings().search_preload_model:
        try:
            get_search_service()
        except Exception:  # keep /health up so the failure is visible, not a crash loop
            log.exception("could not load the search service at startup")
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


@app.get("/search", response_model=SearchResponse)
def search(
    service: Annotated[SearchService, Depends(get_search_service)],
    q: Annotated[str, Query(description="Free-text query, e.g. 'charged twice for one purchase'")],
    mode: SearchMode = SearchMode.hybrid,
    keyword_match: Annotated[
        KeywordMatch, Query(description="'all' requires every term, 'any' matches any term")
    ] = KeywordMatch.all,
    k: Annotated[int, Query(description="Complaints to return (1-100)")] = 10,
    candidates: Annotated[int, Query(description="Chunks per arm before fusion")] = 100,
    rrf_k: Annotated[int, Query(description="Reciprocal Rank Fusion constant")] = 60,
    date_from: date | None = None,
    date_to: date | None = None,
    product_family: str | None = None,
    company: str | None = None,
    state: Annotated[str | None, Query(description="Two-letter state code")] = None,
) -> SearchResponse:
    """Keyword, semantic or hybrid (RRF) search; returns complaints with their best chunk."""
    try:
        params = SearchParams(
            q=q,
            mode=mode,
            keyword_match=keyword_match,
            k=k,
            candidates=candidates,
            rrf_k=rrf_k,
            date_from=date_from,
            date_to=date_to,
            product_family=product_family,
            company=company,
            state=state,
        )
    except ValidationError as exc:
        raise HTTPException(
            status_code=422,
            detail=exc.errors(include_url=False, include_context=False, include_input=False),
        ) from exc
    try:
        return service.search(params)
    except psycopg.errors.QueryCanceled as exc:
        raise HTTPException(
            status_code=504,
            detail="search exceeded the statement timeout; narrow it with filters or "
            "build a vector index (see docs/BENCHMARKS.md)",
        ) from exc
