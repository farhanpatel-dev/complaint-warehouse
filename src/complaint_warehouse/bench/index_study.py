"""Vector index study: HNSW vs IVFFlat, float32 vs halfvec, against an exact baseline.

Method
------
1. `prepare` copies a deterministic sample of embedded chunks (whole complaints, chosen by
   a hash of complaint_id) into bench.chunk_vectors, embeds a fixed query set, and computes
   the exact top-100 neighbours of every query client-side (knn.py).
2. `index` builds one index at a time on that table, records build time, size and any
   memory notices, then sweeps the search-time knob (hnsw.ef_search, ivfflat.probes). Each
   setting runs every query once to warm the cache, then once timed. Recall is measured at
   LIMIT 10 and at LIMIT 100, the candidate count hybrid search asks each arm for.
3. A plain EXPLAIN check before timing fails the run if the planner did not use the index,
   so a number can never silently come from the wrong plan.

Why a sample: on this laptop an HNSW graph over all 5.6M vectors does not fit in memory
(see docs/adr/0004-hnsw-parameters.md), and a parameter sweep needs several builds. The
sample keeps every build in memory, and the same harness runs on the full table with
--table.
"""

from __future__ import annotations

import logging
import tomllib
from dataclasses import asdict, dataclass, field
from typing import Any

import numpy as np
import psycopg
from psycopg import sql

from complaint_warehouse.bench import common, knn, metrics

log = logging.getLogger(__name__)

TABLE = "bench.chunk_vectors"
GT_K = 100
DIM = 768


@dataclass(frozen=True)
class IndexSpec:
    name: str
    method: str  # exact | hnsw | ivfflat
    precision: str = "vector"  # vector | halfvec
    params: dict[str, int] = field(default_factory=dict)

    def expr(self) -> str:
        return "embedding" if self.precision == "vector" else f"(embedding::halfvec({DIM}))"

    def ddl(self, table: str) -> str:
        ops = f"{self.precision}_cosine_ops"
        with_ = ", ".join(f"{k} = {v}" for k, v in self.params.items())
        return (
            f"CREATE INDEX {self.index_name()} ON {table} USING {self.method} "
            f"({self.expr()} {ops}) WITH ({with_})"
        )

    def index_name(self) -> str:
        return f"bench_{self.name}"

    def query(self, table: str) -> str:
        rhs = "%(q)s" if self.precision == "vector" else f"%(q)s::halfvec({DIM})"
        lhs = "embedding" if self.precision == "vector" else f"embedding::halfvec({DIM})"
        return f"SELECT id FROM {table} ORDER BY {lhs} <=> {rhs} LIMIT %(limit)s"

    def sweep(self) -> list[tuple[str | None, int | None]]:
        """(GUC, value) pairs to measure; exact has a single setting."""
        if self.method == "hnsw":
            return [("hnsw.ef_search", v) for v in (10, 20, 40, 100, 200, 400)]
        if self.method == "ivfflat":
            return [("ivfflat.probes", v) for v in (1, 2, 5, 10, 20, 50)]
        return [(None, None)]


def default_specs(rows: int) -> list[IndexSpec]:
    # pgvector's guidance for IVFFlat: lists = rows / 1000 up to 1M rows, sqrt(rows) beyond.
    lists = max(10, rows // 1000 if rows <= 1_000_000 else int(rows**0.5))
    specs = [IndexSpec("exact", "exact")]
    for precision in ("vector", "halfvec"):
        short = "vec" if precision == "vector" else "half"
        for m, efc in ((16, 64), (32, 128)):
            specs.append(
                IndexSpec(
                    f"hnsw_{short}_m{m}_efc{efc}",
                    "hnsw",
                    precision,
                    {"m": m, "ef_construction": efc},
                )
            )
        specs.append(IndexSpec(f"ivfflat_{short}_l{lists}", "ivfflat", precision, {"lists": lists}))
    return specs


# --- prepare ---------------------------------------------------------------------------


def label_queries(conn: psycopg.Connection, n: int) -> list[str]:
    """Distinct CFPB issue and sub-issue names, a deterministic pseudo-random subset.

    They are what consumers chose to describe their problem, so they make realistic short
    queries without me writing hundreds of them by hand.
    """
    rows = conn.execute(
        """
        SELECT label FROM (
            SELECT DISTINCT issue AS label FROM staging.stg_cfpb__complaints
            WHERE issue IS NOT NULL
            UNION
            SELECT DISTINCT sub_issue FROM staging.stg_cfpb__complaints
            WHERE sub_issue IS NOT NULL
        ) AS labels
        ORDER BY md5(label)
        LIMIT %s
        """,
        (n,),
    ).fetchall()
    return [r[0] for r in rows]


def _create_sample(conn: psycopg.Connection, source_table: str, buckets: int) -> None:
    conn.execute(f"DROP TABLE IF EXISTS {TABLE}")
    conn.execute(
        f"""
        CREATE TABLE {TABLE} AS
        SELECT
            (row_number() OVER (ORDER BY complaint_id, chunk_no))::integer AS id,
            complaint_id, chunk_no, tsv, embedding
        FROM {source_table}
        WHERE embedding IS NOT NULL AND (hashint8(complaint_id) & 1023) < %s
        """,
        (buckets,),
    )
    conn.execute(f"ALTER TABLE {TABLE} ADD PRIMARY KEY (id)")
    conn.execute(f"VACUUM (ANALYZE) {TABLE}")


def prepare(
    conn: psycopg.Connection,
    encoder: Any,
    sample_pct: float,
    n_queries: int,
    source_table: str = "search.complaint_text",
    keep_table: bool = False,
) -> dict[str, Any]:
    buckets = round(sample_pct * 1024 / 100)
    conn.execute("CREATE SCHEMA IF NOT EXISTS bench")
    reuse = keep_table and conn.execute(f"SELECT to_regclass('{TABLE}')").fetchone()[0]
    with common.Timer() as t:
        if reuse:
            log.info("keeping the existing %s", TABLE)
        else:
            log.info("sampling %s/1024 of complaints from %s", buckets, source_table)
            _create_sample(conn, source_table, buckets)
    rows = conn.execute(f"SELECT count(*) FROM {TABLE}").fetchone()[0]
    log.info("bench table: %s rows in %.0f s", rows, t.seconds)

    with open(common.QUERIES_FILE, "rb") as f:
        eval_texts = [q["text"] for q in tomllib.load(f)["query"]]
    texts = [("eval", t) for t in eval_texts]
    texts += [("cfpb_label", t) for t in label_queries(conn, max(0, n_queries - len(texts)))]
    vectors = np.stack([encoder.encode_query(t) for _, t in texts]).astype(np.float32)

    conn.execute("DROP TABLE IF EXISTS bench.queries")
    conn.execute(f"""
        CREATE TABLE bench.queries (
            query_id integer PRIMARY KEY, source text NOT NULL, query_text text NOT NULL,
            embedding vector({DIM}) NOT NULL
        )
        """)
    with conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO bench.queries VALUES (%s, %s, %s, %s)",
            [(i, src, t, vectors[i]) for i, (src, t) in enumerate(texts)],
        )

    log.info("exact top-%s for %s queries", GT_K, len(texts))
    with common.Timer() as gt_t:
        ids, dist, scanned = knn.exact_topk(conn, TABLE, vectors, GT_K)
    conn.execute("DROP TABLE IF EXISTS bench.ground_truth")
    conn.execute("""
        CREATE TABLE bench.ground_truth (
            query_id integer, rank integer, id integer, distance double precision,
            PRIMARY KEY (query_id, rank)
        )
        """)
    with conn.cursor() as cur, cur.copy("COPY bench.ground_truth FROM STDIN") as copy:
        for qi in range(ids.shape[0]):
            for r in range(ids.shape[1]):
                copy.write_row((qi, r + 1, int(ids[qi, r]), float(dist[qi, r])))
    info = {
        "source_table": source_table,
        "sample_pct": sample_pct,
        "rows": rows,
        "complaints": conn.execute(f"SELECT count(DISTINCT complaint_id) FROM {TABLE}").fetchone()[
            0
        ],
        "table_size": conn.execute(
            f"SELECT pg_size_pretty(pg_total_relation_size('{TABLE}'))"
        ).fetchone()[0],
        "queries": len(texts),
        "queries_by_source": {
            s: sum(1 for src, _ in texts if src == s) for s in ("eval", "cfpb_label")
        },
        # None when --keep-table reused a sample built earlier.
        "build_seconds": None if reuse else round(t.seconds, 1),
        "ground_truth_seconds": round(gt_t.seconds, 1),
        "ground_truth_rows_scanned": scanned,
        "model": getattr(encoder, "name", None),
    }
    results = common.load_results("index_study")
    results["dataset"] = info
    results.pop("indexes", None)  # a new sample invalidates earlier measurements
    common.save_results("index_study", results)
    return info


# --- index study -------------------------------------------------------------------------


def load_queries(conn: psycopg.Connection) -> tuple[list[np.ndarray], np.ndarray]:
    rows = conn.execute("SELECT embedding FROM bench.queries ORDER BY query_id").fetchall()
    vectors = [knn.as_array(r[0]) for r in rows]
    gt = conn.execute(
        "SELECT array_agg(id ORDER BY rank) FROM bench.ground_truth "
        "GROUP BY query_id ORDER BY query_id"
    ).fetchall()
    return vectors, np.asarray([r[0] for r in gt], dtype=np.int64)


def drop_bench_indexes(conn: psycopg.Connection, table: str) -> None:
    for (name,) in conn.execute(
        """
        SELECT i.relname FROM pg_index x
        JOIN pg_class i ON i.oid = x.indexrelid
        WHERE x.indrelid = %s::regclass AND i.relname LIKE 'bench\\_%%'
        """,
        (table,),
    ).fetchall():
        conn.execute(sql.SQL("DROP INDEX bench.{}").format(sql.Identifier(name)))


def plan_uses(conn: psycopg.Connection, query: str, params: dict, index: str | None) -> bool:
    plan = "\n".join(r[0] for r in conn.execute(f"EXPLAIN {query}", params).fetchall())
    if index is None:
        return "Index Scan using bench_" not in plan
    return f"Index Scan using {index}" in plan


def set_guc(conn: psycopg.Connection, name: str | None, value: int | None) -> None:
    if name is not None:
        conn.execute("SELECT set_config(%s, %s, false)", (name, str(value)))


@dataclass
class Point:
    setting: str | None
    value: int | None
    limit: int
    recall: float
    p50_ms: float
    p95_ms: float
    mean_ms: float
    queries: int
    planner_chose_index: bool = True


def measure(
    conn: psycopg.Connection,
    spec: IndexSpec,
    vectors: list[np.ndarray],
    gt: np.ndarray,
    guc: str | None,
    value: int | None,
    limit: int,
) -> Point:
    set_guc(conn, guc, value)
    query = spec.query(TABLE)
    index = None if spec.method == "exact" else spec.index_name()
    probe = {"q": vectors[0], "limit": limit}
    forced = False
    if not plan_uses(conn, query, probe, index):
        # The planner can prefer a sequential scan, e.g. IVFFlat with many probes, where
        # pgvector's cost estimate approaches a full scan. Record that, then force the
        # index so the setting is still measured; the report marks the point as forced.
        if index is None:
            raise RuntimeError(f"{spec.name}: exact baseline unexpectedly used an index")
        conn.execute("SET enable_seqscan = off")
        forced = True
        if not plan_uses(conn, query, probe, index):
            conn.execute("RESET enable_seqscan")
            raise RuntimeError(f"{spec.name}: index not used even with enable_seqscan off")
        log.warning("%s %s=%s: planner chose a seq scan; index forced", spec.name, guc, value)
    for v in vectors:  # warm-up pass, not timed
        conn.execute(query, {"q": v, "limit": limit}).fetchall()
    latencies, recalls = [], []
    for v, truth in zip(vectors, gt, strict=True):
        with common.Timer() as t:
            found = [r[0] for r in conn.execute(query, {"q": v, "limit": limit}).fetchall()]
        latencies.append(t.seconds * 1000)
        recalls.append(metrics.ann_recall(found, list(truth), limit))
    if forced:
        conn.execute("RESET enable_seqscan")
    return Point(
        guc,
        value,
        limit,
        round(float(np.mean(recalls)), 4),
        round(metrics.percentile(latencies, 50), 2),
        round(metrics.percentile(latencies, 95), 2),
        round(float(np.mean(latencies)), 2),
        len(vectors),
        not forced,
    )


def run_index(
    conn: psycopg.Connection,
    spec: IndexSpec,
    vectors: list[np.ndarray],
    gt: np.ndarray,
    maintenance_work_mem: str,
    parallel_workers: int,
    exact_queries: int,
) -> dict[str, Any]:
    drop_bench_indexes(conn, TABLE)
    result: dict[str, Any] = {"spec": asdict(spec)}
    if spec.method != "exact":
        conn.execute(
            "SELECT set_config('maintenance_work_mem', %s, false)", (maintenance_work_mem,)
        )
        conn.execute(
            "SELECT set_config('max_parallel_maintenance_workers', %s, false)",
            (str(parallel_workers),),
        )
        notices: list[str] = []

        def on_notice(diag: psycopg.errors.Diagnostic) -> None:
            notices.append(diag.message_primary or "")

        conn.add_notice_handler(on_notice)
        log.info("%s: building (%s)", spec.name, spec.ddl(TABLE))
        try:
            with common.Timer() as t:
                conn.execute(spec.ddl(TABLE))
        finally:
            conn.remove_notice_handler(on_notice)
        size = conn.execute(
            "SELECT pg_relation_size(%s::regclass), pg_size_pretty(pg_relation_size(%s::regclass))",
            (f"bench.{spec.index_name()}", f"bench.{spec.index_name()}"),
        ).fetchone()
        result["build"] = {
            "seconds": round(t.seconds, 1),
            "size_bytes": size[0],
            "size": size[1],
            "maintenance_work_mem": maintenance_work_mem,
            "parallel_workers": parallel_workers,
            "notices": notices,
        }
        log.info("%s: built in %.0f s, %s", spec.name, t.seconds, size[1])

    points = []
    for guc, value in spec.sweep():
        for limit in (10, 100):
            if spec.method == "hnsw" and value is not None and value < limit:
                continue  # HNSW returns at most ef_search rows; recall would be capped
            qs, truth = vectors, gt
            if spec.method == "exact":
                qs, truth = vectors[:exact_queries], gt[:exact_queries]
            p = measure(conn, spec, qs, truth, guc, value, limit)
            log.info(
                "%s %s=%s limit=%s: recall %.3f p50 %.1f ms",
                *(spec.name, guc, value, limit, p.recall, p.p50_ms),
            )
            points.append(asdict(p))
    result["points"] = points

    first = spec.sweep()[0] if spec.method == "exact" else _default_setting(spec)
    set_guc(conn, *first)
    plan = common.explain_text(conn, spec.query(TABLE), {"q": vectors[0], "limit": 10})
    common.save_text(common.EXPLAIN_DIR / f"index_{spec.name}.txt", plan)
    return result


def _default_setting(spec: IndexSpec) -> tuple[str, int]:
    return ("hnsw.ef_search", 40) if spec.method == "hnsw" else ("ivfflat.probes", 10)


def run_study(
    conn: psycopg.Connection,
    only: list[str] | None,
    force: bool,
    maintenance_work_mem: str,
    parallel_workers: int,
    exact_queries: int,
) -> dict[str, Any]:
    results = common.load_results("index_study")
    if "dataset" not in results:
        raise SystemExit("run `bench prepare` first")
    results["environment"] = common.environment(conn)
    indexes = results.setdefault("indexes", {})
    vectors, gt = load_queries(conn)
    for spec in default_specs(results["dataset"]["rows"]):
        if only and spec.name not in only:
            continue
        if spec.name in indexes and not force:
            log.info("%s: already measured, skipping (use --force)", spec.name)
            continue
        indexes[spec.name] = run_index(
            conn, spec, vectors, gt, maintenance_work_mem, parallel_workers, exact_queries
        )
        common.save_results("index_study", results)  # keep progress if a later build dies
    drop_bench_indexes(conn, TABLE)
    return results


# --- GIN (full-text) -----------------------------------------------------------------------


def run_gin(conn: psycopg.Connection, maintenance_work_mem: str) -> dict[str, Any]:
    """Keyword search on the same sample with and without a GIN index on tsv.

    The keyword arm is the other half of hybrid search; this shows what GIN buys and what
    OR matching (keyword_match = 'any') costs, since OR matches far more chunks to rank.
    """
    with open(common.QUERIES_FILE, "rb") as f:
        texts = [q["text"] for q in tomllib.load(f)["query"]]
    drop_bench_indexes(conn, TABLE)
    conn.execute("SELECT set_config('maintenance_work_mem', %s, false)", (maintenance_work_mem,))
    queries = {
        "all": "websearch_to_tsquery('english', %(q)s)",
        "any": "replace(websearch_to_tsquery('english', %(q)s)::text, ' & ', ' | ')::tsquery",
    }

    def sweep(label: str) -> dict[str, Any]:
        out = {}
        for match, tsq in queries.items():
            # A cast is not allowed on a function in FROM, hence the subquery; Postgres
            # pulls it up, so the tsquery is still an index condition for GIN.
            source = f"FROM {TABLE} AS t, (SELECT {tsq} AS q) AS x WHERE t.tsv @@ x.q"
            stmt = f"SELECT t.id {source} ORDER BY ts_rank_cd(t.tsv, x.q) DESC, t.id LIMIT 100"
            count_stmt = f"SELECT count(*) {source}"
            matches = [conn.execute(count_stmt, {"q": t}).fetchone()[0] for t in texts]
            for t in texts:  # warm-up
                conn.execute(stmt, {"q": t}).fetchall()
            lat = []
            for t in texts:
                with common.Timer() as tm:
                    conn.execute(stmt, {"q": t}).fetchall()
                lat.append(tm.seconds * 1000)
            out[match] = {
                "p50_ms": round(metrics.percentile(lat, 50), 2),
                "p95_ms": round(metrics.percentile(lat, 95), 2),
                "mean_ms": round(float(np.mean(lat)), 2),
                "median_matching_chunks": int(np.median(matches)),
                "queries_with_no_match": sum(1 for m in matches if m == 0),
            }
            plan = common.explain_text(conn, stmt, {"q": texts[0]})
            common.save_text(common.EXPLAIN_DIR / f"gin_{label}_{match}.txt", plan)
            log.info("gin %s %s: %s", label, match, out[match])
        return out

    result: dict[str, Any] = {"queries": len(texts), "no_index": sweep("no_index")}
    with common.Timer() as t:
        conn.execute(f"CREATE INDEX bench_gin_tsv ON {TABLE} USING gin (tsv)")
    size = conn.execute(
        "SELECT pg_relation_size('bench.bench_gin_tsv'), "
        "pg_size_pretty(pg_relation_size('bench.bench_gin_tsv'))"
    ).fetchone()
    result["build"] = {"seconds": round(t.seconds, 1), "size_bytes": size[0], "size": size[1]}
    result["gin"] = sweep("gin")
    drop_bench_indexes(conn, TABLE)
    results = common.load_results("index_study")
    results["gin"] = result
    common.save_results("index_study", results)
    return result
