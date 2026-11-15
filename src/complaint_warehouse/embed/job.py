"""Embed pending chunks of search.complaint_text, resumable after any failure.

Each iteration is one transaction:

    fetch   next `fetch_size` chunks with embedding IS NULL after the checkpoint key
    encode  on the GPU/CPU (outside any open transaction)
    write   COPY vectors into a temp table, UPDATE the target from it, move the checkpoint

so a crash, OOM kill or Ctrl+C loses at most one uncommitted batch, and a rerun continues
from the last committed key. Rerunning a finished job is a no-op apart from chunks that
appeared since.

The UPDATE only applies a vector if the chunk text still hashes to what was embedded. dbt
rebuilds a narrative's chunks with delete+insert when its text changes; without the guard
a vector computed from the old text could land on the new row.
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from importlib import resources
from typing import Any

import numpy as np
import psycopg
from pgvector.psycopg import register_vector
from psycopg import sql

from complaint_warehouse.db import connect
from complaint_warehouse.embed.model import Encoder
from complaint_warehouse.ingest.loader import ensure_schema as ensure_ops_schema
from complaint_warehouse.ingest.loader import load_run

log = logging.getLogger(__name__)

DEFAULT_TABLE = "search.complaint_text"
START_KEY = (-1, -1)
_TABLE_RE = re.compile(r"^[a-z_][a-z0-9_]*\.[a-z_][a-z0-9_]*$")


@dataclass
class EmbedStats:
    fetched: int = 0
    embedded: int = 0
    skipped_changed: int = 0
    batches: int = 0
    wall_seconds: float = 0.0
    # write runs on the writer thread, overlapped with fetch/encode; wait is the time the
    # main loop spent blocked on it. wait near zero means the GPU is the bottleneck.
    seconds: dict[str, float] = field(
        default_factory=lambda: {"fetch": 0.0, "encode": 0.0, "write": 0.0, "wait": 0.0}
    )

    @property
    def rows_per_second(self) -> float:
        return self.embedded / self.wall_seconds if self.wall_seconds else 0.0


def ensure_schema(conn: psycopg.Connection) -> None:
    ensure_ops_schema(conn)
    conn.execute(resources.files("complaint_warehouse.embed").joinpath("sql/ddl.sql").read_text())


def table_ident(table: str) -> sql.Identifier:
    if not _TABLE_RE.match(table):
        raise ValueError(f"not a schema-qualified table name: {table!r}")
    schema, name = table.split(".")
    return sql.Identifier(schema, name)


def embedding_dim(conn: psycopg.Connection, table: str) -> int:
    row = conn.execute(
        "SELECT format_type(atttypid, atttypmod) FROM pg_attribute "
        "WHERE attrelid = %s::regclass AND attname = 'embedding' AND NOT attisdropped",
        (table,),
    ).fetchone()
    match = re.fullmatch(r"vector\((\d+)\)", row[0]) if row else None
    if not match:
        raise ValueError(f"{table}.embedding is not a vector(n) column")
    return int(match.group(1))


def read_checkpoint(conn: psycopg.Connection, table: str, model: str) -> tuple[int, int]:
    row = conn.execute(
        "SELECT last_complaint_id, last_chunk_no, status FROM ops.embedding_checkpoints "
        "WHERE target_table = %s AND model = %s",
        (table, model),
    ).fetchone()
    if row is None or row[2] == "complete":
        return START_KEY
    return (row[0], row[1])


def other_models(conn: psycopg.Connection, table: str, model: str) -> list[str]:
    """Models that have embedded into this table before. Mixing vector spaces in one
    column would make every distance meaningless, so the job refuses until `reset`."""
    return [
        r[0]
        for r in conn.execute(
            "SELECT model FROM ops.embedding_checkpoints WHERE target_table = %s AND model <> %s",
            (table, model),
        )
    ]


def _save_checkpoint(
    conn: psycopg.Connection,
    table: str,
    model: str,
    key: tuple[int, int],
    embedded: int,
    status: str,
    load_id: int,
) -> None:
    conn.execute(
        """
        INSERT INTO ops.embedding_checkpoints AS c
            (target_table, model, last_complaint_id, last_chunk_no, rows_embedded, status,
             load_id, updated_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, now())
        ON CONFLICT (target_table, model) DO UPDATE SET
            last_complaint_id = excluded.last_complaint_id,
            last_chunk_no = excluded.last_chunk_no,
            rows_embedded = c.rows_embedded + excluded.rows_embedded,
            status = excluded.status,
            load_id = excluded.load_id,
            updated_at = now()
        """,
        (table, model, key[0], key[1], embedded, status, load_id),
    )


def _start_sweep(conn: psycopg.Connection, table: str, model: str, load_id: int) -> None:
    conn.execute(
        """
        INSERT INTO ops.embedding_checkpoints (target_table, model, status, load_id)
        VALUES (%s, %s, 'running', %s)
        ON CONFLICT (target_table, model) DO UPDATE SET
            rows_embedded = 0, status = 'running', load_id = excluded.load_id, updated_at = now()
        """,
        (table, model, load_id),
    )


def count_pending(conn: psycopg.Connection, table: str) -> int:
    query = sql.SQL("SELECT count(*) FROM {} WHERE embedding IS NULL").format(table_ident(table))
    return conn.execute(query).fetchone()[0]


class _Writer:
    """Writes embedded batches on its own connection, in submission order.

    Runs on a single worker thread so the GPU can encode batch k+1 while batch k is being
    written. One thread means checkpoints still commit strictly in key order.
    """

    def __init__(
        self,
        connect_fn: Callable[[], psycopg.Connection],
        table: str,
        dim: int,
        model: str,
        load_id: int,
    ) -> None:
        self.table, self.model, self.load_id = table, model, load_id
        self.conn = connect_fn()
        register_vector(self.conn)
        self.conn.execute(
            sql.SQL(
                "CREATE TEMP TABLE IF NOT EXISTS embed_batch (complaint_id BIGINT, "
                "chunk_no INTEGER, text_md5 TEXT, embedding vector({})) ON COMMIT DELETE ROWS"
            ).format(sql.Literal(dim))
        )
        self.conn.commit()
        self.update_sql = sql.SQL(
            "UPDATE {} AS t SET embedding = b.embedding, embedding_model = %s, "
            "embedded_at = now() FROM embed_batch AS b "
            "WHERE t.complaint_id = b.complaint_id AND t.chunk_no = b.chunk_no "
            "AND t.embedding IS NULL AND md5(t.narrative) = b.text_md5"
        ).format(table_ident(table))

    def write(self, rows: list[tuple], vectors: np.ndarray) -> tuple[int, float]:
        t0 = time.monotonic()
        try:
            with self.conn.cursor() as cur:
                with cur.copy(
                    "COPY embed_batch (complaint_id, chunk_no, text_md5, embedding) "
                    "FROM STDIN WITH (FORMAT BINARY)"
                ) as copy:
                    copy.set_types(["int8", "int4", "text", "vector"])
                    for row, vec in zip(rows, vectors, strict=True):
                        copy.write_row((row[0], row[1], row[3], vec))
                cur.execute(self.update_sql, (self.model,))
                applied = cur.rowcount
            key = (rows[-1][0], rows[-1][1])
            _save_checkpoint(
                self.conn, self.table, self.model, key, applied, "running", self.load_id
            )
            self.conn.commit()
        except BaseException:
            self.conn.rollback()
            raise
        return applied, time.monotonic() - t0

    def complete(self) -> None:
        _save_checkpoint(self.conn, self.table, self.model, START_KEY, 0, "complete", self.load_id)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()


def run(
    conn: psycopg.Connection,
    encoder: Encoder,
    *,
    table: str = DEFAULT_TABLE,
    fetch_size: int = 2048,
    limit: int | None = None,
    progress_every_seconds: float = 30.0,
    connect_fn: Callable[[], psycopg.Connection] | None = None,
) -> EmbedStats:
    """Embed pending chunks until none are left or `limit` chunks have been processed."""
    ident = table_ident(table)
    ensure_schema(conn)
    conn.commit()

    dim = embedding_dim(conn, table)
    if dim != encoder.dim:
        raise ValueError(f"{table}.embedding is vector({dim}) but {encoder.name} has {encoder.dim}")
    if others := other_models(conn, table, encoder.name):
        raise RuntimeError(
            f"{table} has embeddings from {others}; run `reset` before switching to "
            f"{encoder.name}"
        )

    fetch_sql = sql.SQL(
        "SELECT complaint_id, chunk_no, narrative, md5(narrative) FROM {} "
        "WHERE embedding IS NULL AND (complaint_id, chunk_no) > (%s, %s) "
        "ORDER BY complaint_id, chunk_no LIMIT %s"
    ).format(ident)

    params: dict[str, Any] = {"table": table, "model": encoder.name, "limit": limit}
    stats = EmbedStats()
    with load_run(conn, "embed", params) as lr:
        key = read_checkpoint(conn, table, encoder.name)
        if key == START_KEY:
            _start_sweep(conn, table, encoder.name, lr.load_id)
        pending = count_pending(conn, table)
        conn.commit()
        log.info("embed %s with %s: %s pending, resuming after %s", table, encoder.name,
                 f"{pending:,}", key)  # fmt: skip
        target = min(pending, limit) if limit is not None else pending
        # Throughput excludes setup (model load, pending count) so short samples are honest.
        started = last_report = time.monotonic()

        writer = _Writer(connect_fn or connect, table, dim, encoder.name, lr.load_id)

        def collect(future: Future, batch_size: int) -> None:
            t0 = time.monotonic()
            applied, write_seconds = future.result()  # re-raises a failed write
            stats.seconds["wait"] += time.monotonic() - t0
            stats.seconds["write"] += write_seconds
            stats.embedded += applied
            stats.skipped_changed += batch_size - applied
            stats.batches += 1
            lr.stats.read, lr.stats.updated = stats.fetched, stats.embedded

        try:
            with ThreadPoolExecutor(max_workers=1, thread_name_prefix="embed-writer") as pool:
                in_flight: Future | None = None
                in_flight_size = 0
                exhausted = False
                while limit is None or stats.fetched < limit:
                    want = fetch_size if limit is None else min(fetch_size, limit - stats.fetched)
                    t0 = time.monotonic()
                    rows = conn.execute(fetch_sql, (key[0], key[1], want)).fetchall()
                    conn.commit()  # never hold a transaction open across the encode step
                    t1 = time.monotonic()
                    stats.seconds["fetch"] += t1 - t0
                    if not rows:
                        exhausted = True
                        break
                    stats.fetched += len(rows)
                    key = (rows[-1][0], rows[-1][1])

                    vectors = encoder.encode_documents([r[2] for r in rows])
                    stats.seconds["encode"] += time.monotonic() - t1

                    if in_flight is not None:
                        collect(in_flight, in_flight_size)
                    in_flight, in_flight_size = pool.submit(writer.write, rows, vectors), len(rows)

                    if time.monotonic() - last_report >= progress_every_seconds:
                        last_report = time.monotonic()
                        stats.wall_seconds = last_report - started
                        rate = stats.rows_per_second
                        eta_h = (target - stats.fetched) / rate / 3600 if rate else float("nan")
                        log.info(
                            "%s/%s embedded (%.0f/s, ETA %.1fh) fetch=%.0fs encode=%.0fs "
                            "write=%.0fs (waited %.0fs)",
                            f"{stats.embedded:,}", f"{target:,}", rate, eta_h,
                            stats.seconds["fetch"], stats.seconds["encode"],
                            stats.seconds["write"], stats.seconds["wait"],
                        )  # fmt: skip

                if in_flight is not None:
                    collect(in_flight, in_flight_size)
                if exhausted:
                    writer.complete()
        finally:
            writer.close()
    stats.wall_seconds = time.monotonic() - started
    return stats


def reset(conn: psycopg.Connection, table: str, keep_model: str | None) -> int:
    """Clear vectors from every model except `keep_model` (all of them if None)."""
    ident = table_ident(table)
    ensure_schema(conn)
    cur = conn.execute(
        sql.SQL(
            "UPDATE {} SET embedding = NULL, embedding_model = NULL, embedded_at = NULL "
            "WHERE embedding IS NOT NULL AND embedding_model IS DISTINCT FROM %s"
        ).format(ident),
        (keep_model,),
    )
    conn.execute(
        "DELETE FROM ops.embedding_checkpoints WHERE target_table = %s "
        "AND model IS DISTINCT FROM %s",
        (table, keep_model),
    )
    conn.commit()
    return cur.rowcount
