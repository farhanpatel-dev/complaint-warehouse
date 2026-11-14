"""Client for the CFPB complaint search API.

The API is backed by Elasticsearch. Deep paging uses `search_after` with the sort values of
the last hit (`<created_date epoch ms>_<complaint_id>`), which stays stable while new
complaints are indexed, unlike offset paging. Date bounds are inclusive on both ends.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator
from datetime import date
from typing import Any

import httpx

from complaint_warehouse.ingest.http import make_client, request_with_retry

log = logging.getLogger(__name__)


class CfpbApiClient:
    def __init__(
        self,
        base_url: str,
        user_agent: str,
        *,
        page_size: int = 1000,
        timeout: float = 60.0,
        max_retries: int = 5,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        self.base_url = base_url
        self.page_size = page_size
        self.max_retries = max_retries
        self._client = make_client(user_agent, timeout, transport)
        self._sleep_kw = {"sleep": sleep} if sleep else {}

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> CfpbApiClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _get(self, params: dict[str, Any]) -> dict[str, Any]:
        response = request_with_retry(
            self._client,
            "GET",
            self.base_url,
            params=params,
            max_retries=self.max_retries,
            **self._sleep_kw,
        )
        return response.json()

    @staticmethod
    def _window(start: date, end: date) -> dict[str, Any]:
        return {
            "date_received_min": start.isoformat(),
            "date_received_max": end.isoformat(),
            "no_aggs": "true",
        }

    def count(self, start: date, end: date) -> int:
        data = self._get(self._window(start, end) | {"size": 0})
        return int(data["hits"]["total"]["value"])

    def iter_hits(self, start: date, end: date) -> Iterator[dict[str, Any]]:
        """Yield every hit with date_received in [start, end], oldest first."""
        params = self._window(start, end) | {"size": self.page_size, "sort": "created_date_asc"}
        search_after: str | None = None
        while True:
            page_params = params | ({"search_after": search_after} if search_after else {})
            hits = self._get(page_params)["hits"]["hits"]
            yield from hits
            if len(hits) < self.page_size:
                return
            search_after = "_".join(str(v) for v in hits[-1]["sort"])
