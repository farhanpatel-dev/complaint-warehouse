"""End-to-end search: real model, dbt-built search tables, embedded chunks.

Runs in CI after the fixture sample is ingested, built and embedded, and locally against
the full warehouse (`make test-e2e`). Each test picks its target from the data and filters
to that complaint's day, so it is fast on 1k chunks and on 5.6M alike, with or without a
vector index.
"""

from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.e2e

RRF_K = 60


@pytest.fixture(scope="module")
def service():
    if not os.getenv("DATABASE_URL"):
        pytest.skip("DATABASE_URL not set")
    pytest.importorskip("sentence_transformers")
    from complaint_warehouse.config import get_settings
    from complaint_warehouse.db import close_pool, get_pool
    from complaint_warehouse.embed.model import load_encoder
    from complaint_warehouse.search.service import SearchService

    pool = get_pool()
    with pool.connection() as conn:
        ready = conn.execute(
            "SELECT to_regprocedure('search.hybrid_search(text, vector, integer, integer, "
            "double precision, double precision, integer, date, date, text, text, text)') "
            "IS NOT NULL AND EXISTS (SELECT 1 FROM search.complaint_text "
            "WHERE embedding IS NOT NULL)"
        ).fetchone()[0]
    if not ready:
        pytest.skip("search tables, function or embeddings not built")
    yield SearchService(pool, load_encoder(get_settings()))
    close_pool()


@pytest.fixture(scope="module")
def target(service):
    """A real chunk of a few dozen words with a vector, and its complaint's attributes."""
    with service.pool.connection() as conn:
        row = conn.execute("""
            SELECT t.complaint_id, t.narrative, f.date_received, f.product_family
            FROM search.complaint_text AS t
            JOIN search.complaint_facets AS f USING (complaint_id)
            WHERE t.chunk_no = 0 AND t.embedding IS NOT NULL
              AND array_length(regexp_split_to_array(t.narrative, '\\s+'), 1) BETWEEN 40 AND 200
            ORDER BY t.complaint_id
            LIMIT 1
            """).fetchone()
    assert row, "no suitable chunk found"
    return {"id": row[0], "text": row[1], "day": row[2], "family": row[3]}


def _params(target, **over):
    from complaint_warehouse.search.service import SearchParams

    # Whole words only: websearch_to_tsquery ANDs every term, so a word cut in half would
    # stop the keyword arm from matching even the chunk the query came from.
    query = " ".join(target["text"].split()[:40])
    base = {"q": query, "date_from": target["day"], "date_to": target["day"]}
    return SearchParams(**(base | over))


def test_semantic_search_finds_a_chunk_from_its_own_text(service, target):
    resp = service.search(_params(target, mode="semantic", k=3))
    ids = [h.complaint_id for h in resp.results]
    assert target["id"] in ids
    top = resp.results[ids.index(target["id"])]
    assert top.keyword_rank is None and top.cosine_similarity > 0.8


def test_keyword_search_finds_a_chunk_from_its_own_words(service, target):
    words = " ".join(target["text"].split()[:12])
    resp = service.search(_params(target, q=words, mode="keyword", k=10))
    hits = {h.complaint_id: h for h in resp.results}
    assert target["id"] in hits
    assert hits[target["id"]].semantic_rank is None
    assert "**" in hits[target["id"]].snippet, "keyword hits carry a highlighted snippet"


def test_hybrid_scores_follow_reciprocal_rank_fusion(service, target):
    resp = service.search(_params(target, k=10))
    assert resp.results
    scores = [h.score for h in resp.results]
    assert scores == sorted(scores, reverse=True)
    for h in resp.results:
        expected = sum(1 / (RRF_K + r) for r in (h.keyword_rank, h.semantic_rank) if r)
        assert h.score == pytest.approx(expected)
    assert resp.results[0].complaint_id == target["id"]
    assert resp.results[0].keyword_rank and resp.results[0].semantic_rank


def test_filters_are_respected(service, target):
    resp = service.search(_params(target, product_family=target["family"], k=20))
    assert resp.results
    assert {h.product_family for h in resp.results} == {target["family"]}
    assert {h.date_received for h in resp.results} == {target["day"]}


def test_http_endpoint_end_to_end(service, target):
    from fastapi.testclient import TestClient

    from complaint_warehouse.api import main

    main.app.dependency_overrides[main.get_search_service] = lambda: service
    try:
        resp = TestClient(main.app).get(
            "/search",
            params={
                "q": " ".join(target["text"].split()[:30]),
                "date_from": target["day"].isoformat(),
                "date_to": target["day"].isoformat(),
            },
        )
    finally:
        main.app.dependency_overrides.clear()
    assert resp.status_code == 200
    body = resp.json()
    assert body["results"] and body["results"][0]["complaint_id"] == target["id"]
    assert set(body["took_ms"]) == {"embed", "db", "total"}
