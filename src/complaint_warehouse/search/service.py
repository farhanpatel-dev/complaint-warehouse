"""Search service: embeds the query and calls search.hybrid_search in one round trip.

All ranking logic lives in the SQL function (dbt/macros/search/hybrid_search.sql). This
layer validates input, maps a search mode to arm weights, embeds the query, and shapes the
response. Keyword-only searches skip the model entirely.
"""

from __future__ import annotations

import time
from datetime import date
from enum import StrEnum
from typing import Any

import numpy as np
from psycopg_pool import ConnectionPool
from pydantic import BaseModel, Field, model_validator

from complaint_warehouse.embed.model import Encoder


class SearchMode(StrEnum):
    hybrid = "hybrid"
    keyword = "keyword"
    semantic = "semantic"


# Arm weights per mode. A weight of 0 switches the arm off inside the SQL function.
MODE_WEIGHTS: dict[SearchMode, tuple[float, float]] = {
    SearchMode.hybrid: (1.0, 1.0),
    SearchMode.keyword: (1.0, 0.0),
    SearchMode.semantic: (0.0, 1.0),
}


class SearchParams(BaseModel):
    q: str = Field(min_length=1, max_length=500, description="Free-text query.")
    mode: SearchMode = SearchMode.hybrid
    k: int = Field(default=10, ge=1, le=100, description="Complaints to return.")
    candidates: int = Field(
        default=100, ge=1, le=1000, description="Chunks each arm keeps before fusion."
    )
    rrf_k: int = Field(default=60, ge=1, le=1000, description="RRF smoothing constant.")
    date_from: date | None = None
    date_to: date | None = None
    product_family: str | None = Field(default=None, max_length=100)
    company: str | None = Field(default=None, max_length=200)
    state: str | None = Field(default=None, pattern=r"^[A-Z]{2}$")

    @model_validator(mode="after")
    def _check(self) -> SearchParams:
        if not self.q.strip():
            raise ValueError("q must contain non-whitespace characters")
        if self.date_from and self.date_to and self.date_from > self.date_to:
            raise ValueError("date_from must be on or before date_to")
        if self.candidates < self.k:
            raise ValueError("candidates must be >= k")
        return self


class SearchHit(BaseModel):
    complaint_id: int
    score: float
    keyword_rank: int | None
    semantic_rank: int | None
    keyword_score: float | None
    cosine_similarity: float | None
    date_received: date
    company: str
    product: str
    sub_product: str
    product_family: str
    issue: str | None
    state: str
    response_type: str
    timely: bool | None
    chunk_no: int
    snippet: str


class SearchResponse(BaseModel):
    query: str
    mode: SearchMode
    took_ms: dict[str, float]
    results: list[SearchHit]


SEARCH_SQL = """
SELECT
    h.complaint_id, h.score, h.keyword_rank, h.semantic_rank, h.keyword_score,
    h.cosine_similarity, f.date_received, f.company_name, f.product, f.sub_product,
    f.product_family, f.issue, f.state_code, f.response_type, f.timely_flag, h.best_chunk_no,
    CASE
        WHEN h.keyword_rank IS NOT NULL THEN ts_headline(
            'english', t.narrative, websearch_to_tsquery('english', %(q)s),
            'MaxWords=45, MinWords=20, MaxFragments=2, StartSel=**, StopSel=**'
        )
        ELSE left(t.narrative, 300)
    END AS snippet
FROM search.hybrid_search(
    query_text => %(q)s,
    query_embedding => %(embedding)s,
    match_count => %(k)s,
    candidate_count => %(candidates)s,
    keyword_weight => %(kw)s,
    semantic_weight => %(sw)s,
    rrf_k => %(rrf_k)s,
    date_from => %(date_from)s,
    date_to => %(date_to)s,
    product_family => %(product_family)s,
    company_name => %(company)s,
    state_code => %(state)s
) AS h
JOIN search.complaint_facets AS f ON f.complaint_id = h.complaint_id
JOIN search.complaint_text AS t
    ON t.complaint_id = h.complaint_id AND t.chunk_no = h.best_chunk_no
ORDER BY h.score DESC, h.complaint_id
"""


class SearchService:
    def __init__(self, pool: ConnectionPool, encoder: Encoder) -> None:
        self.pool = pool
        self.encoder = encoder

    def search(self, params: SearchParams) -> SearchResponse:
        started = time.perf_counter()
        kw, sw = MODE_WEIGHTS[params.mode]
        if sw > 0:
            embedding = self.encoder.encode_query(params.q)
        else:
            # The semantic arm is switched off, but the argument is still typed vector(n).
            embedding = np.zeros(self.encoder.dim, dtype=np.float32)
        embedded = time.perf_counter()

        args: dict[str, Any] = {
            "q": params.q,
            "embedding": embedding,
            "k": params.k,
            "candidates": params.candidates,
            "kw": kw,
            "sw": sw,
            "rrf_k": params.rrf_k,
            "date_from": params.date_from,
            "date_to": params.date_to,
            "product_family": params.product_family,
            "company": params.company,
            "state": params.state,
        }
        with self.pool.connection() as conn:
            rows = conn.execute(SEARCH_SQL, args).fetchall()
        finished = time.perf_counter()

        hits = [
            SearchHit(
                complaint_id=r[0],
                score=r[1],
                keyword_rank=r[2],
                semantic_rank=r[3],
                keyword_score=r[4],
                cosine_similarity=r[5],
                date_received=r[6],
                company=r[7],
                product=r[8],
                sub_product=r[9],
                product_family=r[10],
                issue=r[11],
                state=r[12],
                response_type=r[13],
                timely=r[14],
                chunk_no=r[15],
                snippet=r[16],
            )
            for r in rows
        ]
        return SearchResponse(
            query=params.q,
            mode=params.mode,
            took_ms={
                "embed": round((embedded - started) * 1000, 1),
                "db": round((finished - embedded) * 1000, 1),
                "total": round((finished - started) * 1000, 1),
            },
            results=hits,
        )
