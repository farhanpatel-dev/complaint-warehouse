"""Idempotent loading into the raw schema.

Every batch goes through the same path:

1. COPY the batch into a session temp table (fast, no per-row round trips).
2. One INSERT ... ON CONFLICT (complaint_id) DO UPDATE ... WHERE record_hash IS DISTINCT FROM
   merges it into the target. New ids are inserted, changed rows are updated, and identical
   rows are skipped entirely, so re-running a load writes nothing and creates no dead tuples.

Functions here never commit; the caller owns the transaction boundary.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date
from importlib import resources
from typing import Any, TypeVar

import psycopg

from complaint_warehouse.ingest.records import COMPLAINT_FIELDS, ComplaintRecord, NarrativeRecord

log = logging.getLogger(__name__)

T = TypeVar("T")

# Identifiers below come from module constants, never from input, so string building is safe.
_COMPLAINT_COLS = (*COMPLAINT_FIELDS, "source", "record_hash", "load_id")
_NARRATIVE_COLS = (
    "complaint_id",
    "date_received",
    "narrative",
    "source_file",
    "record_hash",
    "load_id",
)


@dataclass
class MergeStats:
    read: int = 0
    inserted: int = 0
    updated: int = 0

    @property
    def unchanged(self) -> int:
        return self.read - self.inserted - self.updated

    def __iadd__(self, other: MergeStats) -> MergeStats:
        self.read += other.read
        self.inserted += other.inserted
        self.updated += other.updated
        return self


def ensure_schema(conn: psycopg.Connection) -> None:
    ddl = resources.files("complaint_warehouse.ingest").joinpath("sql/ddl.sql").read_text()
    conn.execute(ddl)


def batched(items: Iterable[T], size: int) -> Iterator[list[T]]:
    batch: list[T] = []
    for item in items:
        batch.append(item)
        if len(batch) >= size:
            yield batch
            batch = []
    if batch:
        yield batch


def _merge_sql(target: str, temp: str, cols: Sequence[str]) -> str:
    col_list = ", ".join(cols)
    updates = ", ".join(f"{c} = excluded.{c}" for c in cols if c != "complaint_id")
    # DISTINCT ON guards against the same id appearing twice in one batch, which would
    # otherwise make ON CONFLICT DO UPDATE fail ("cannot affect row a second time").
    return f"""
        WITH src AS (
            SELECT DISTINCT ON (complaint_id) {col_list}
            FROM {temp}
            ORDER BY complaint_id
        ),
        merged AS (
            INSERT INTO {target} AS t ({col_list})
            SELECT {col_list} FROM src
            ON CONFLICT (complaint_id) DO UPDATE
            SET {updates}, last_changed_at = now()
            WHERE t.record_hash IS DISTINCT FROM excluded.record_hash
            RETURNING (xmax = 0) AS inserted
        )
        SELECT
            count(*) FILTER (WHERE inserted),
            count(*) FILTER (WHERE NOT inserted),
            (SELECT count(*) FROM src)
        FROM merged
    """


def _copy_and_merge(
    conn: psycopg.Connection,
    target: str,
    temp: str,
    cols: Sequence[str],
    rows: Iterable[Sequence[Any]],
) -> MergeStats:
    with conn.cursor() as cur:
        cur.execute(
            f"CREATE TEMP TABLE IF NOT EXISTS {temp} "
            f"(LIKE {target} INCLUDING DEFAULTS) ON COMMIT DELETE ROWS"
        )
        cur.execute(f"TRUNCATE {temp}")
        with cur.copy(f"COPY {temp} ({', '.join(cols)}) FROM STDIN") as copy:
            for row in rows:
                copy.write_row(row)
        inserted, updated, distinct = cur.execute(_merge_sql(target, temp, cols)).fetchone()
    return MergeStats(read=distinct, inserted=inserted, updated=updated)


def merge_complaints(
    conn: psycopg.Connection, records: Sequence[ComplaintRecord], source: str, load_id: int
) -> MergeStats:
    rows = ((*r.as_row(), source, r.record_hash, load_id) for r in records)
    return _copy_and_merge(conn, "raw.complaints", "tmp_complaints", _COMPLAINT_COLS, rows)


def merge_narratives(
    conn: psycopg.Connection, records: Sequence[NarrativeRecord], source_file: str, load_id: int
) -> MergeStats:
    rows = (
        (r.complaint_id, r.date_received, r.narrative, source_file, r.record_hash, load_id)
        for r in records
    )
    return _copy_and_merge(
        conn, "raw.complaint_narratives", "tmp_narratives", _NARRATIVE_COLS, rows
    )


# --- watermarks ---------------------------------------------------------------------------


def get_watermark(conn: psycopg.Connection, key: str) -> date | None:
    row = conn.execute(
        "SELECT high_watermark FROM ops.watermarks WHERE watermark_key = %s", (key,)
    ).fetchone()
    return row[0] if row else None


def advance_watermark(conn: psycopg.Connection, key: str, value: date) -> None:
    """Move the watermark forward only; a manual backfill of old dates never rewinds it."""
    conn.execute(
        """
        INSERT INTO ops.watermarks (watermark_key, high_watermark) VALUES (%s, %s)
        ON CONFLICT (watermark_key) DO UPDATE
        SET high_watermark = GREATEST(ops.watermarks.high_watermark, excluded.high_watermark),
            updated_at = now()
        """,
        (key, value),
    )


# --- run log ------------------------------------------------------------------------------


@dataclass
class LoadRun:
    load_id: int
    stats: MergeStats


@contextmanager
def load_run(conn: psycopg.Connection, source: str, params: dict[str, Any]) -> Iterator[LoadRun]:
    """Record a run in ops.load_runs and mark it succeeded or failed on exit.

    The 'running' row is committed up front so a crashed run stays visible in the log.
    """
    load_id = conn.execute(
        "INSERT INTO ops.load_runs (source, params) VALUES (%s, %s) RETURNING load_id",
        (source, json.dumps(params, default=str)),
    ).fetchone()[0]
    conn.commit()
    run = LoadRun(load_id=load_id, stats=MergeStats())
    try:
        yield run
    except BaseException as exc:
        conn.rollback()
        conn.execute(
            "UPDATE ops.load_runs SET status = 'failed', finished_at = now(), error = %s, "
            "rows_read = %s, rows_inserted = %s, rows_updated = %s WHERE load_id = %s",
            (repr(exc)[:2000], run.stats.read, run.stats.inserted, run.stats.updated, load_id),
        )
        conn.commit()
        raise
    conn.execute(
        "UPDATE ops.load_runs SET status = 'succeeded', finished_at = now(), "
        "rows_read = %s, rows_inserted = %s, rows_updated = %s WHERE load_id = %s",
        (run.stats.read, run.stats.inserted, run.stats.updated, load_id),
    )
    conn.commit()
