"""HTTP helper with bounded exponential backoff for the CFPB endpoints."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import Any

import httpx

log = logging.getLogger(__name__)

RETRYABLE_STATUS = {429, 500, 502, 503, 504}


def make_client(
    user_agent: str, timeout: float, transport: httpx.BaseTransport | None = None
) -> httpx.Client:
    return httpx.Client(
        headers={"User-Agent": user_agent, "Accept": "application/json"},
        timeout=timeout,
        follow_redirects=True,
        transport=transport,
    )


def request_with_retry(
    client: httpx.Client,
    method: str,
    url: str,
    *,
    max_retries: int,
    backoff_seconds: float = 1.0,
    sleep: Callable[[float], None] = time.sleep,
    **kwargs: Any,
) -> httpx.Response:
    """Send a request, retrying transport errors and 429/5xx with exponential backoff.

    Honours a numeric Retry-After header. Other 4xx responses raise immediately: they mean the
    request itself is wrong and retrying would only hide the bug.
    """
    attempt = 0
    while True:
        try:
            response = client.request(method, url, **kwargs)
            if response.status_code not in RETRYABLE_STATUS:
                response.raise_for_status()
                return response
            reason = f"HTTP {response.status_code}"
            retry_after = response.headers.get("Retry-After")
        except httpx.TransportError as exc:
            reason, retry_after = type(exc).__name__, None

        if attempt >= max_retries:
            raise RuntimeError(f"{method} {url} failed after {attempt + 1} attempts ({reason})")
        delay = backoff_seconds * (2**attempt)
        if retry_after and retry_after.isdigit():
            delay = max(delay, float(retry_after))
        log.warning("retrying %s %s in %.1fs (%s)", method, url, delay, reason)
        sleep(delay)
        attempt += 1
