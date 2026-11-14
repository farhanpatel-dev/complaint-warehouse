"""Load orchestration for the three CFPB sources.

Units of atomicity (and therefore of resume) differ per source on purpose:

* api        - one date window. The window's rows and the watermark advance commit together,
               so a crash re-reads at most one window.
* bulk       - one batch. The file is 19M+ rows; holding one transaction open for all of it
               would bloat WAL and block vacuum. Batches are idempotent, so a crash just reruns.
* narratives - one archive file, recorded in ops.ingested_files by SHA-256 so a rerun skips
               files that are already fully loaded.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

import psycopg

from complaint_warehouse.config import Settings
from complaint_warehouse.ingest.loader import (
    LoadRun,
    MergeStats,
    advance_watermark,
    batched,
    get_watermark,
    load_run,
    merge_complaints,
    merge_narratives,
)
from complaint_warehouse.ingest.records import (
    ComplaintRecord,
    NarrativeRecord,
    RecordError,
    from_api_hit,
    from_csv_row,
)
from complaint_warehouse.ingest.sources.api import CfpbApiClient
from complaint_warehouse.ingest.sources.archives import Archive
from complaint_warehouse.ingest.sources.files import download, iter_zipped_csv, sha256_file

log = logging.getLogger(__name__)

WATERMARK_KEY = "complaints"

Pair = tuple[ComplaintRecord, NarrativeRecord | None]


# --- pure planning helpers (unit tested) --------------------------------------------------


def date_windows(start: date, end: date, days: int) -> list[tuple[date, date]]:
    """Split the inclusive range [start, end] into consecutive inclusive windows."""
    if days < 1:
        raise ValueError("days must be >= 1")
    windows = []
    cursor = start
    while cursor <= end:
        window_end = min(cursor + timedelta(days=days - 1), end)
        windows.append((cursor, window_end))
        cursor = window_end + timedelta(days=1)
    return windows


def plan_incremental_range(
    *,
    watermark: date | None,
    lookback_days: int,
    default_start: date,
    today: date,
    start: date | None = None,
    end: date | None = None,
) -> tuple[date, date]:
    """Choose the date range for an incremental run.

    Explicit --start/--end win. Otherwise resume from the watermark minus a lookback, because
    CFPB updates complaints after they are first published (the company response and the
    timely flag arrive days later), and those rows keep their original date_received.
    """
    end = end or today
    if start is None:
        start = default_start if watermark is None else watermark - timedelta(days=lookback_days)
    if start > end:
        raise ValueError(f"start {start} is after end {end}")
    return start, end


def in_range(d: date, start: date | None, end: date | None) -> bool:
    return (start is None or d >= start) and (end is None or d <= end)


# --- shared batch loop --------------------------------------------------------------------


@dataclass
class ParseCounter:
    rejected: int = 0


def _parse(rows: Iterable[dict], parser, counter: ParseCounter) -> Iterator[Pair]:
    for row in rows:
        try:
            yield parser(row)
        except (RecordError, ValueError) as exc:
            counter.rejected += 1
            if counter.rejected <= 10:
                log.warning("rejected row: %s", exc)


def _load_batch(
    conn: psycopg.Connection,
    batch: list[Pair],
    run: LoadRun,
    *,
    source: str,
    write_complaints: bool,
    write_narratives: bool,
    source_file: str | None = None,
) -> MergeStats:
    stats = MergeStats()
    if write_complaints:
        stats += merge_complaints(conn, [c for c, _ in batch], source, run.load_id)
    narratives = [n for _, n in batch if n is not None]
    if write_narratives and narratives:
        n_stats = merge_narratives(conn, narratives, source_file or source, run.load_id)
        if not write_complaints:
            stats += n_stats
    return stats


# --- sources ------------------------------------------------------------------------------


def run_api(
    conn: psycopg.Connection,
    client: CfpbApiClient,
    *,
    start: date,
    end: date,
    window_days: int,
    batch_size: int,
) -> LoadRun:
    params = {"start": start, "end": end, "window_days": window_days}
    with load_run(conn, "api", params) as run:
        for w_start, w_end in date_windows(start, end, window_days):
            expected = client.count(w_start, w_end)
            counter = ParseCounter()
            window = MergeStats()
            pairs = _parse(client.iter_hits(w_start, w_end), from_api_hit, counter)
            for batch in batched(pairs, batch_size):
                window += _load_batch(
                    conn, batch, run, source="api", write_complaints=True, write_narratives=True
                )
            if window.read + counter.rejected < expected:
                # Not fatal: the index can change while paging. The next run's lookback
                # re-reads this window anyway.
                log.warning(
                    "window %s..%s: API reported %d, read %d", w_start, w_end, expected, window.read
                )
            advance_watermark(conn, WATERMARK_KEY, w_end)
            conn.commit()
            run.stats += window
            log.info(
                "api %s..%s read=%d inserted=%d updated=%d unchanged=%d rejected=%d",
                w_start,
                w_end,
                window.read,
                window.inserted,
                window.updated,
                window.unchanged,
                counter.rejected,
            )
    return run


def _register_file(
    conn: psycopg.Connection, path: Path, source: str, sha: str, rows_read: int, load_id: int
) -> None:
    conn.execute(
        """
        INSERT INTO ops.ingested_files (file_name, source, sha256, size_bytes, rows_read, load_id)
        VALUES (%s, %s, %s, %s, %s, %s)
        ON CONFLICT (file_name) DO UPDATE
        SET sha256 = excluded.sha256, size_bytes = excluded.size_bytes,
            rows_read = excluded.rows_read, load_id = excluded.load_id, loaded_at = now()
        """,
        (path.name, source, sha, path.stat().st_size, rows_read, load_id),
    )


def run_bulk(
    conn: psycopg.Connection,
    path: Path,
    *,
    start: date | None,
    end: date | None,
    batch_size: int,
) -> LoadRun:
    params = {"file": path.name, "start": start, "end": end}
    sha = sha256_file(path)
    max_seen: date | None = None
    with load_run(conn, "bulk", params) as run:
        counter = ParseCounter()
        pairs = (
            p
            for p in _parse(iter_zipped_csv(path), from_csv_row, counter)
            if in_range(p[0].date_received, start, end)
        )
        for batch in batched(pairs, batch_size):
            run.stats += _load_batch(
                conn,
                batch,
                run,
                source="bulk",
                write_complaints=True,
                write_narratives=True,
                source_file=path.name,
            )
            batch_max = max(c.date_received for c, _ in batch)
            max_seen = batch_max if max_seen is None else max(max_seen, batch_max)
            conn.commit()
            log.info("bulk %s: %d rows loaded so far", path.name, run.stats.read)
        if max_seen is not None:
            # The bulk file is a full snapshot, so it also moves the incremental watermark:
            # after a backfill, `ingest api` continues from where the file ends.
            advance_watermark(conn, WATERMARK_KEY, max_seen)
        _register_file(conn, path, "bulk", sha, run.stats.read, run.load_id)
        conn.commit()
        if counter.rejected:
            log.warning("bulk %s: rejected %d rows", path.name, counter.rejected)
    return run


def already_loaded(conn: psycopg.Connection, file_name: str, sha: str) -> bool:
    row = conn.execute(
        "SELECT sha256 FROM ops.ingested_files WHERE file_name = %s", (file_name,)
    ).fetchone()
    return row is not None and row[0] == sha


def load_narrative_file(
    conn: psycopg.Connection,
    path: Path,
    *,
    start: date | None,
    end: date | None,
    batch_size: int,
    force: bool = False,
) -> LoadRun | None:
    sha = sha256_file(path)
    full_file = start is None and end is None
    if full_file and not force and already_loaded(conn, path.name, sha):
        log.info("%s already loaded (sha256 match), skipping", path.name)
        return None
    with load_run(conn, "narratives", {"file": path.name, "start": start, "end": end}) as run:
        counter = ParseCounter()
        pairs = (
            p
            for p in _parse(iter_zipped_csv(path), from_csv_row, counter)
            if p[1] is not None and in_range(p[0].date_received, start, end)
        )
        for batch in batched(pairs, batch_size):
            run.stats += _load_batch(
                conn,
                batch,
                run,
                source="narratives",
                write_complaints=False,
                write_narratives=True,
                source_file=path.name,
            )
            conn.commit()
        # Only a full-file load is registered; a date-filtered load must not block a later
        # full load of the same file.
        if full_file:
            _register_file(conn, path, "narratives", sha, run.stats.read, run.load_id)
            conn.commit()
        log.info(
            "narratives %s: read=%d inserted=%d updated=%d rejected=%d",
            path.name,
            run.stats.read,
            run.stats.inserted,
            run.stats.updated,
            counter.rejected,
        )
    return run


def run_narratives(
    conn: psycopg.Connection,
    archives: list[Archive],
    settings: Settings,
    *,
    start: date | None,
    end: date | None,
    force: bool = False,
) -> list[LoadRun]:
    runs = []
    for archive in archives:
        path = download(
            archive.url(settings.cfpb_archive_base_url),
            settings.data_dir / "raw" / "narratives" / archive.file_name,
            user_agent=settings.cfpb_user_agent,
            timeout=settings.http_timeout_seconds,
            max_retries=settings.http_max_retries,
        )
        run = load_narrative_file(
            conn, path, start=start, end=end, batch_size=settings.ingest_batch_size, force=force
        )
        if run is not None:
            runs.append(run)
    return runs


def bulk_file_path(settings: Settings) -> Path:
    return settings.data_dir / "raw" / "bulk" / "complaints.csv.zip"


def fetch_bulk(settings: Settings) -> Path:
    return download(
        settings.cfpb_bulk_url,
        bulk_file_path(settings),
        user_agent=settings.cfpb_user_agent,
        timeout=settings.http_timeout_seconds,
        max_retries=settings.http_max_retries,
    )


def current_watermark(conn: psycopg.Connection) -> date | None:
    return get_watermark(conn, WATERMARK_KEY)
