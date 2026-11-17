"""Exact k-nearest-neighbour ground truth, computed client-side in one pass over the table.

Asking Postgres for the exact top-k means one full scan per query (about 100 s each on the
full corpus). Streaming the vectors once and scoring every query against each batch with a
matrix product gives the exact answer for all queries in a single scan, in bounded memory.
"""

from __future__ import annotations

import numpy as np
import psycopg
from psycopg import sql


def as_array(v: object) -> np.ndarray:
    """pgvector returns numpy arrays over text and Vector objects over binary transfer."""
    to_numpy = getattr(v, "to_numpy", None)
    return np.asarray(to_numpy() if to_numpy else v, dtype=np.float32)


def normalise(x: np.ndarray) -> np.ndarray:
    x = np.asarray(x, dtype=np.float32)
    norms = np.linalg.norm(x, axis=-1, keepdims=True)
    return x / np.where(norms == 0, 1, norms)


def merge_topk(
    best_ids: np.ndarray,
    best_sims: np.ndarray,
    ids: np.ndarray,
    sims: np.ndarray,
    k: int,
) -> tuple[np.ndarray, np.ndarray]:
    """Merge a batch's similarities (nq x b) into the running top-k (nq x k), unordered."""
    cand_sims = np.concatenate([best_sims, sims], axis=1)
    cand_ids = np.concatenate([best_ids, np.broadcast_to(ids, sims.shape)], axis=1)
    keep = min(k, cand_sims.shape[1])
    part = np.argpartition(-cand_sims, keep - 1, axis=1)[:, :keep]
    return np.take_along_axis(cand_ids, part, axis=1), np.take_along_axis(cand_sims, part, axis=1)


def sort_topk(ids: np.ndarray, sims: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Order each row by cosine distance ascending, ties by id, as ORDER BY ... , id would."""
    out_ids = np.empty_like(ids)
    out_dist = np.empty_like(sims, dtype=np.float64)
    for row in range(ids.shape[0]):
        order = np.lexsort((ids[row], -sims[row]))
        out_ids[row] = ids[row, order]
        out_dist[row] = 1.0 - sims[row, order]
    return out_ids, out_dist


def exact_topk(
    conn: psycopg.Connection,
    table: str,
    queries: np.ndarray,
    k: int,
    *,
    id_column: str = "id",
    column: str = "embedding",
    batch_size: int = 20_000,
) -> tuple[np.ndarray, np.ndarray, int]:
    """Exact cosine top-k for every query. Returns (ids, distances, rows_scanned)."""
    q = normalise(queries)
    best_ids = np.empty((q.shape[0], 0), dtype=np.int64)
    best_sims = np.empty((q.shape[0], 0), dtype=np.float32)
    scanned = 0
    schema, _, name = table.rpartition(".")
    stmt = sql.SQL("SELECT {id}, {col} FROM {table} WHERE {col} IS NOT NULL").format(
        id=sql.Identifier(id_column),
        col=sql.Identifier(column),
        table=sql.Identifier(schema, name) if schema else sql.Identifier(name),
    )
    with conn.transaction(), conn.cursor(name="exact_knn_scan", binary=True) as cur:
        cur.itersize = batch_size
        cur.execute(stmt)
        while rows := cur.fetchmany(batch_size):
            ids = np.fromiter((r[0] for r in rows), dtype=np.int64, count=len(rows))
            x = normalise(np.stack([as_array(r[1]) for r in rows]))
            best_ids, best_sims = merge_topk(best_ids, best_sims, ids, q @ x.T, k)
            scanned += len(rows)
    ids, dist = sort_topk(best_ids, best_sims)
    return ids, dist, scanned
