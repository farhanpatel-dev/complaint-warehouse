import os

import pytest
from fastapi.testclient import TestClient

from complaint_warehouse.api import main


@pytest.fixture
def client():
    return TestClient(main.app)


def test_health_ok(client, monkeypatch):
    monkeypatch.setattr(main, "check_database", lambda: {"postgres": "16.4", "pgvector": "0.8.0"})
    resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["database"]["pgvector"] == "0.8.0"


def test_health_degraded_when_db_down(client, monkeypatch):
    def boom():
        raise ConnectionError("db down")

    monkeypatch.setattr(main, "check_database", boom)
    resp = client.get("/health")
    assert resp.status_code == 503
    assert resp.json()["status"] == "degraded"


@pytest.mark.integration
@pytest.mark.skipif(not os.getenv("DATABASE_URL"), reason="DATABASE_URL not set")
def test_health_against_real_db(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["database"]["pgvector"] != "missing"
