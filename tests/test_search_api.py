"""/search endpoint and SearchService, without a database or model."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import date

import numpy as np
import psycopg
import pytest
from fastapi.testclient import TestClient

from complaint_warehouse.api import main
from complaint_warehouse.search.service import (
    MODE_WEIGHTS,
    SearchMode,
    SearchParams,
    SearchResponse,
    SearchService,
)


class FakeService:
    def __init__(self, exc: Exception | None = None):
        self.calls: list[SearchParams] = []
        self.exc = exc

    def search(self, params: SearchParams) -> SearchResponse:
        self.calls.append(params)
        if self.exc:
            raise self.exc
        return SearchResponse(query=params.q, mode=params.mode, took_ms={"total": 1.0}, results=[])


@pytest.fixture
def fake():
    service = FakeService()
    main.app.dependency_overrides[main.get_search_service] = lambda: service
    yield service
    main.app.dependency_overrides.clear()


@pytest.fixture
def client():
    return TestClient(main.app)


def test_search_passes_validated_params(client, fake):
    resp = client.get(
        "/search",
        params={
            "q": "charged twice",
            "mode": "keyword",
            "k": 5,
            "date_from": "2025-01-01",
            "date_to": "2025-03-31",
            "product_family": "Credit cards",
            "state": "TX",
        },
    )
    assert resp.status_code == 200
    assert resp.json()["mode"] == "keyword"
    (p,) = fake.calls
    assert p.mode is SearchMode.keyword and p.k == 5 and p.state == "TX"
    assert p.date_from == date(2025, 1, 1) and p.product_family == "Credit cards"


@pytest.mark.parametrize(
    "params",
    [
        {},  # q missing
        {"q": "   "},
        {"q": "x" * 501},
        {"q": "ok", "k": 0},
        {"q": "ok", "k": 101},
        {"q": "ok", "k": 50, "candidates": 20},
        {"q": "ok", "mode": "fuzzy"},
        {"q": "ok", "keyword_match": "some"},
        {"q": "ok", "state": "Texas"},
        {"q": "ok", "date_from": "2025-02-01", "date_to": "2025-01-01"},
    ],
)
def test_invalid_requests_are_422(client, fake, params):
    assert client.get("/search", params=params).status_code == 422
    assert fake.calls == []


def test_keyword_match_defaults_to_all_and_accepts_any(client, fake):
    client.get("/search", params={"q": "late fee"})
    client.get("/search", params={"q": "late fee", "keyword_match": "any"})
    assert [p.keyword_match.value for p in fake.calls] == ["all", "any"]


def test_statement_timeout_maps_to_504(client):
    service = FakeService(exc=psycopg.errors.QueryCanceled("canceling statement"))
    main.app.dependency_overrides[main.get_search_service] = lambda: service
    try:
        resp = client.get("/search", params={"q": "anything"})
    finally:
        main.app.dependency_overrides.clear()
    assert resp.status_code == 504


def test_modes_switch_arms_off_with_zero_weight():
    assert MODE_WEIGHTS[SearchMode.hybrid] == (1.0, 1.0)
    assert MODE_WEIGHTS[SearchMode.keyword] == (1.0, 0.0)
    assert MODE_WEIGHTS[SearchMode.semantic] == (0.0, 1.0)


# --- SearchService with a fake pool ----------------------------------------------------


class RecordingPool:
    def __init__(self):
        self.args = None

    @contextmanager
    def connection(self):
        pool = self

        class Conn:
            def execute(self, _sql, args):
                pool.args = args

                class Cur:
                    def fetchall(self):
                        return []

                return Cur()

        yield Conn()


class CountingEncoder:
    name, dim = "fake", 4

    def __init__(self):
        self.queries: list[str] = []

    def encode_documents(self, texts):
        raise AssertionError("not used by search")

    def encode_query(self, text):
        self.queries.append(text)
        return np.ones(self.dim, dtype=np.float32) / 2


def test_keyword_mode_never_runs_the_model():
    pool, enc = RecordingPool(), CountingEncoder()
    SearchService(pool, enc).search(SearchParams(q="late fee", mode=SearchMode.keyword))
    assert enc.queries == []
    assert pool.args["kw"] == 1.0 and pool.args["sw"] == 0.0
    assert pool.args["embedding"].shape == (4,) and not pool.args["embedding"].any()


def test_hybrid_mode_embeds_the_query_once_and_reports_timings():
    pool, enc = RecordingPool(), CountingEncoder()
    resp = SearchService(pool, enc).search(SearchParams(q="late fee", company="ACME"))
    assert enc.queries == ["late fee"]
    assert pool.args["company"] == "ACME" and (pool.args["kw"], pool.args["sw"]) == (1.0, 1.0)
    assert pool.args["keyword_match"] == "all"
    assert set(resp.took_ms) == {"embed", "db", "total"}
