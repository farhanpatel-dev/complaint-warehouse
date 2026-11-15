"""Embedding job: checkpointing, resume after failure, stale-text guard, model safety.

A deterministic fake encoder stands in for the real model, so these run without torch or
a model download. Integration tests use their own small table, never search.complaint_text.
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Callable, Sequence

import numpy as np
import pytest

from complaint_warehouse.embed import job

DIM = 8


class FakeEncoder:
    def __init__(self, name: str = "fake-model", dim: int = DIM, on_call: Callable | None = None):
        self.name = name
        self.dim = dim
        self.calls = 0
        self.on_call = on_call

    def encode_documents(self, texts: Sequence[str]) -> np.ndarray:
        self.calls += 1
        if self.on_call:
            self.on_call(self.calls, texts)
        return np.stack([vector_for(t, self.dim) for t in texts])

    def encode_query(self, text: str) -> np.ndarray:
        return vector_for(text, self.dim)


def vector_for(text: str, dim: int = DIM) -> np.ndarray:
    seed = int.from_bytes(hashlib.md5(text.encode()).digest()[:4], "little")
    v = np.random.default_rng(seed).standard_normal(dim).astype(np.float32)
    return v / np.linalg.norm(v)


# --- unit -------------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["complaint_text", "a.b; drop table x", "Search.T", "a.b.c"])
def test_table_ident_rejects_unqualified_or_unsafe_names(name):
    with pytest.raises(ValueError):
        job.table_ident(name)


def test_stats_rate_uses_wall_clock_not_summed_stage_times():
    # Writes overlap encoding, so summing stage times would understate throughput.
    stats = job.EmbedStats(embedded=300, wall_seconds=2.0)
    stats.seconds.update(fetch=0.5, encode=1.5, write=1.5)
    assert stats.rows_per_second == 150


def test_fake_vectors_are_normalised_and_deterministic():
    assert np.allclose(vector_for("abc"), vector_for("abc"))
    assert np.isclose(np.linalg.norm(vector_for("abc")), 1.0)


# --- integration ------------------------------------------------------------------------

N_COMPLAINTS = 10  # 10 complaints x (1..3 chunks) = 20 chunks


@pytest.fixture
def chunk_table(db_conn):
    table = f"search.embed_test_{os.getpid()}"
    ident = job.table_ident(table)
    from psycopg import sql

    db_conn.execute(
        sql.SQL(
            "CREATE TABLE {} (complaint_id BIGINT, chunk_no INTEGER, narrative TEXT, "
            "embedding vector({}), embedding_model TEXT, embedded_at TIMESTAMPTZ, "
            "PRIMARY KEY (complaint_id, chunk_no))"
        ).format(ident, sql.Literal(DIM))
    )
    db_conn.execute(
        sql.SQL(
            "INSERT INTO {} (complaint_id, chunk_no, narrative) "
            "SELECT c, k, 'complaint ' || c || ' chunk ' || k "
            "FROM generate_series(1, %s) c, generate_series(0, 2) k WHERE k <= c %% 3"
        ).format(ident),
        (N_COMPLAINTS,),
    )
    db_conn.commit()
    from pgvector.psycopg import register_vector

    register_vector(db_conn)  # so assertions can read vectors back as Vector objects
    yield table
    db_conn.rollback()
    db_conn.execute(sql.SQL("DROP TABLE IF EXISTS {}").format(ident))
    db_conn.execute("DELETE FROM ops.embedding_checkpoints WHERE target_table = %s", (table,))
    db_conn.execute(
        "DELETE FROM ops.load_runs WHERE source = 'embed' AND params->>'table' = %s", (table,)
    )
    db_conn.commit()


def _counts(conn, table):
    from psycopg import sql

    return conn.execute(
        sql.SQL("SELECT count(*), count(embedding) FROM {}").format(job.table_ident(table))
    ).fetchone()


def _checkpoint(conn, table):
    return conn.execute(
        "SELECT last_complaint_id, last_chunk_no, status, rows_embedded "
        "FROM ops.embedding_checkpoints WHERE target_table = %s",
        (table,),
    ).fetchone()


@pytest.mark.integration
def test_run_embeds_everything_and_rerun_is_a_noop(db_conn, chunk_table):
    total, _ = _counts(db_conn, chunk_table)
    stats = job.run(db_conn, FakeEncoder(), table=chunk_table, fetch_size=6)
    assert stats.embedded == total == 20
    assert _counts(db_conn, chunk_table) == (total, total)
    assert _checkpoint(db_conn, chunk_table)[2] == "complete"

    from psycopg import sql

    text, emb, model = db_conn.execute(
        sql.SQL(
            "SELECT narrative, embedding, embedding_model FROM {} "
            "WHERE complaint_id = 2 AND chunk_no = 1"
        ).format(job.table_ident(chunk_table))
    ).fetchone()
    assert model == "fake-model"
    assert np.allclose(np.asarray(emb.to_list()), vector_for(text), atol=1e-6)

    again = job.run(db_conn, FakeEncoder(), table=chunk_table, fetch_size=6)
    assert again.embedded == 0 and again.fetched == 0


@pytest.mark.integration
def test_resumes_from_checkpoint_after_a_crash(db_conn, chunk_table):
    def crash_on_third_batch(call, _texts):
        if call == 3:
            raise RuntimeError("simulated OOM")

    with pytest.raises(RuntimeError, match="simulated OOM"):
        job.run(db_conn, FakeEncoder(on_call=crash_on_third_batch), table=chunk_table, fetch_size=5)

    # Two batches committed with their checkpoint; the third rolled back.
    assert _counts(db_conn, chunk_table) == (20, 10)
    last_id, last_chunk, status, swept = _checkpoint(db_conn, chunk_table)
    # Key order: (1,0) (1,1) (2,0) (2,1) (2,2) (3,0) (4,0) (4,1) (5,0) (5,1) ...
    assert (last_id, last_chunk) == (5, 1)
    assert status == "running" and swept == 10
    failed = db_conn.execute(
        "SELECT status FROM ops.load_runs WHERE source = 'embed' AND params->>'table' = %s "
        "ORDER BY load_id DESC LIMIT 1",
        (chunk_table,),
    ).fetchone()[0]
    assert failed == "failed"

    stats = job.run(db_conn, FakeEncoder(), table=chunk_table, fetch_size=5)
    assert stats.fetched == 10, "resume must start after the checkpoint, not rescan"
    assert _counts(db_conn, chunk_table) == (20, 20)
    assert _checkpoint(db_conn, chunk_table)[2:] == ("complete", 20)


@pytest.mark.integration
def test_vector_is_not_applied_if_the_text_changed_meanwhile(db_conn, chunk_table):
    """Simulates dbt re-chunking a narrative while its old text is being embedded."""
    from psycopg import sql

    from complaint_warehouse.db import connect

    def rewrite_first_chunk(call, _texts):
        if call == 1:
            with connect() as other:
                other.execute(
                    sql.SQL(
                        "UPDATE {} SET narrative = 'rewritten' "
                        "WHERE complaint_id = 1 AND chunk_no = 0"
                    ).format(job.table_ident(chunk_table))
                )

    stats = job.run(
        db_conn, FakeEncoder(on_call=rewrite_first_chunk), table=chunk_table, fetch_size=50
    )
    assert stats.skipped_changed == 1 and stats.embedded == 19

    # The skipped chunk is still pending and the next sweep embeds the new text.
    job.run(db_conn, FakeEncoder(), table=chunk_table, fetch_size=50)
    emb = db_conn.execute(
        sql.SQL("SELECT embedding FROM {} WHERE complaint_id = 1 AND chunk_no = 0").format(
            job.table_ident(chunk_table)
        )
    ).fetchone()[0]
    assert np.allclose(np.asarray(emb.to_list()), vector_for("rewritten"), atol=1e-6)


@pytest.mark.integration
def test_limit_stops_early_and_keeps_the_sweep_open(db_conn, chunk_table):
    stats = job.run(db_conn, FakeEncoder(), table=chunk_table, fetch_size=5, limit=7)
    assert stats.embedded == 7
    assert _checkpoint(db_conn, chunk_table)[2] == "running"
    job.run(db_conn, FakeEncoder(), table=chunk_table, fetch_size=5)
    assert _counts(db_conn, chunk_table) == (20, 20)


@pytest.mark.integration
def test_refuses_wrong_dimension_and_mixed_models(db_conn, chunk_table):
    with pytest.raises(ValueError, match="vector"):
        job.run(db_conn, FakeEncoder(dim=DIM * 2), table=chunk_table)

    job.run(db_conn, FakeEncoder("model-a"), table=chunk_table, limit=4)
    with pytest.raises(RuntimeError, match="reset"):
        job.run(db_conn, FakeEncoder("model-b"), table=chunk_table)

    assert job.reset(db_conn, chunk_table, keep_model="model-b") == 4
    stats = job.run(db_conn, FakeEncoder("model-b"), table=chunk_table)
    assert stats.embedded == 20
