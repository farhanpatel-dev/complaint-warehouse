import json
from datetime import date

import httpx
import pytest

from complaint_warehouse.ingest.http import make_client, request_with_retry
from complaint_warehouse.ingest.sources.api import CfpbApiClient
from complaint_warehouse.ingest.sources.files import download, iter_zipped_csv
from tests.conftest import csv_row, write_zip

API = "https://api.test/search/"


def _hit(i: int) -> dict:
    return {
        "_source": {"complaint_id": str(i), "date_received": "2026-01-01"},
        "sort": [1000 + i, str(i)],
    }


def test_api_pages_with_search_after():
    data = [_hit(i) for i in range(5)]
    seen_params = []

    def handler(request: httpx.Request) -> httpx.Response:
        params = dict(request.url.params)
        seen_params.append(params)
        if params.get("size") == "0":
            return httpx.Response(200, json={"hits": {"total": {"value": 5}, "hits": []}})
        start = 0
        if "search_after" in params:
            last_id = params["search_after"].split("_")[1]
            start = int(last_id) + 1
        page = data[start : start + int(params["size"])]
        return httpx.Response(200, json={"hits": {"total": {"value": 5}, "hits": page}})

    client = CfpbApiClient(API, "test-agent", page_size=2, transport=httpx.MockTransport(handler))
    assert client.count(date(2026, 1, 1), date(2026, 1, 1)) == 5
    ids = [
        h["_source"]["complaint_id"] for h in client.iter_hits(date(2026, 1, 1), date(2026, 1, 1))
    ]
    assert ids == ["0", "1", "2", "3", "4"]
    page_calls = [p for p in seen_params if p.get("size") != "0"]
    assert [p.get("search_after") for p in page_calls] == [None, "1001_1", "1003_3"]
    assert page_calls[0]["date_received_min"] == "2026-01-01"
    assert page_calls[0]["sort"] == "created_date_asc"


def test_retry_on_429_then_success_honours_retry_after():
    calls, sleeps = [], []

    def handler(request):
        calls.append(1)
        if len(calls) < 3:
            return httpx.Response(429, headers={"Retry-After": "7"})
        return httpx.Response(200, json={"ok": True})

    with make_client("ua", 5, httpx.MockTransport(handler)) as client:
        resp = request_with_retry(client, "GET", API, max_retries=5, sleep=sleeps.append)
    assert resp.json() == {"ok": True}
    assert sleeps == [7.0, 7.0]


def test_retry_gives_up():
    transport = httpx.MockTransport(lambda r: httpx.Response(503))
    with (
        make_client("ua", 5, transport) as client,
        pytest.raises(RuntimeError, match="after 3 attempts"),
    ):
        request_with_retry(client, "GET", API, max_retries=2, sleep=lambda s: None)


def test_client_error_is_not_retried():
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(400, json={"field": ["invalid"]})

    with (
        make_client("ua", 5, httpx.MockTransport(handler)) as client,
        pytest.raises(httpx.HTTPStatusError),
    ):
        request_with_retry(client, "GET", API, max_retries=5, sleep=lambda s: None)
    assert len(calls) == 1


def test_iter_zipped_csv(tmp_path):
    path = write_zip(tmp_path / "a.zip", [csv_row(1), csv_row(2, narrative="x" * 200_000)])
    rows = list(iter_zipped_csv(path))
    assert [r["Complaint ID"] for r in rows] == ["1", "2"]
    assert len(rows[1]["Consumer complaint narrative"]) == 200_000  # beyond csv's default limit


def _file_server(body: bytes, last_modified: str = "Sat, 14 Nov 2026 10:00:00 GMT"):
    gets = []

    def handler(request: httpx.Request) -> httpx.Response:
        headers = {"Content-Length": str(len(body)), "Last-Modified": last_modified}
        if request.method == "HEAD":
            return httpx.Response(200, headers=headers)
        gets.append(request.headers.get("Range"))
        rng = request.headers.get("Range")
        if rng:
            offset = int(rng.removeprefix("bytes=").rstrip("-"))
            part = body[offset:]
            return httpx.Response(206, content=part, headers={"Content-Length": str(len(part))})
        return httpx.Response(200, content=body, headers=headers)

    return httpx.MockTransport(handler), gets


def test_download_caches_and_skips_when_unchanged(tmp_path):
    transport, gets = _file_server(b"0123456789")
    dest = tmp_path / "f.zip"
    download("https://files.test/f.zip", dest, user_agent="ua", transport=transport)
    download("https://files.test/f.zip", dest, user_agent="ua", transport=transport)
    assert dest.read_bytes() == b"0123456789"
    assert len(gets) == 1
    meta = json.loads((tmp_path / "f.zip.meta.json").read_text())
    assert meta["content_length"] == "10"


def test_download_resumes_partial_file(tmp_path):
    transport, gets = _file_server(b"0123456789")
    dest = tmp_path / "f.zip"
    (tmp_path / "f.zip.part").write_bytes(b"0123")
    download("https://files.test/f.zip", dest, user_agent="ua", transport=transport)
    assert dest.read_bytes() == b"0123456789"
    assert gets == ["bytes=4-"]
    assert not (tmp_path / "f.zip.part").exists()
