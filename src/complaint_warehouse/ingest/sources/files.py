"""Download and stream the CFPB zipped CSV files (bulk export and narrative archives)."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import logging
import zipfile
from collections.abc import Iterator
from pathlib import Path

import httpx

from complaint_warehouse.ingest.http import make_client, request_with_retry

log = logging.getLogger(__name__)

# Narratives can exceed csv's default 128 KiB field limit. sys.maxsize overflows a C long on
# Windows, so use the largest 32-bit value instead.
csv.field_size_limit(2**31 - 1)

CHUNK_BYTES = 1 << 20


def _remote_meta(response: httpx.Response) -> dict[str, str | None]:
    return {
        "content_length": response.headers.get("Content-Length"),
        "last_modified": response.headers.get("Last-Modified"),
        "etag": response.headers.get("ETag"),
    }


def download(
    url: str,
    dest: Path,
    *,
    user_agent: str,
    timeout: float = 60.0,
    max_retries: int = 5,
    transport: httpx.BaseTransport | None = None,
) -> Path:
    """Download `url` to `dest`, skipping if the cached copy matches the remote file.

    A sidecar `<dest>.meta.json` stores Content-Length / Last-Modified / ETag from the last
    complete download, so a daily-changing file (the bulk export) is re-fetched while a frozen
    one (the archives) is not. Partial downloads resume with an HTTP Range request.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    meta_path = dest.with_name(dest.name + ".meta.json")
    part = dest.with_name(dest.name + ".part")

    with make_client(user_agent, timeout, transport) as client:
        head = request_with_retry(client, "HEAD", url, max_retries=max_retries)
        remote = _remote_meta(head)
        if dest.exists() and meta_path.exists() and json.loads(meta_path.read_text()) == remote:
            log.info("cached %s is current, skipping download", dest.name)
            return dest

        offset = part.stat().st_size if part.exists() else 0
        headers = {"Range": f"bytes={offset}-"} if offset else {}
        with client.stream("GET", url, headers=headers) as response:
            response.raise_for_status()
            resumed = offset > 0 and response.status_code == 206
            log.info("downloading %s%s", url, f" (resuming at {offset} bytes)" if resumed else "")
            with part.open("ab" if resumed else "wb") as fh:
                for chunk in response.iter_bytes(CHUNK_BYTES):
                    fh.write(chunk)

    expected = remote["content_length"]
    if expected is not None and part.stat().st_size != int(expected):
        raise OSError(f"{part.name}: got {part.stat().st_size} bytes, expected {expected}")
    part.replace(dest)
    meta_path.write_text(json.dumps(remote))
    return dest


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(CHUNK_BYTES), b""):
            digest.update(chunk)
    return digest.hexdigest()


def iter_zipped_csv(path: Path) -> Iterator[dict[str, str]]:
    """Stream rows of the first CSV inside a zip without extracting it to disk."""
    with zipfile.ZipFile(path) as zf:
        members = [n for n in zf.namelist() if n.lower().endswith(".csv")]
        if not members:
            raise ValueError(f"{path} contains no CSV file")
        with zf.open(members[0]) as raw:
            text = io.TextIOWrapper(raw, encoding="utf-8-sig", errors="replace", newline="")
            yield from csv.DictReader(text)
