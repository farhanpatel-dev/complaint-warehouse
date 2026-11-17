"""Search-quality evaluation: keyword vs semantic vs hybrid on labelled queries.

Every system is a call to search.hybrid_search, the same function the API uses, with the
labelled window as a date filter. The filter keeps every arm exact (no ANN index is
involved), so the comparison measures ranking quality alone, not index approximation.

Labels come from the CFPB issue/sub-issue metadata (see bench/queries/eval_queries.toml).

The RRF constant and arm-weight sweep is computed from the arm ranks one hybrid call
returns (metrics.rrf_fuse mirrors the SQL), after checking that the Python fusion
reproduces the SQL ordering exactly. It is a sensitivity analysis on the same 52 queries,
not a tuned result: picking the best cell would overfit them.
"""

from __future__ import annotations

import logging
import tomllib
from dataclasses import dataclass
from datetime import date
from typing import Any

import numpy as np
import psycopg

from complaint_warehouse.bench import common, metrics

log = logging.getLogger(__name__)

K = 10
DEEP_K = 100
CANDIDATES = 100

# name -> (keyword_weight, semantic_weight, keyword_match)
SYSTEMS: dict[str, tuple[float, float, str]] = {
    "keyword_all": (1.0, 0.0, "all"),
    "keyword_any": (1.0, 0.0, "any"),
    "semantic": (0.0, 1.0, "all"),
    "hybrid_all": (1.0, 1.0, "all"),
    "hybrid_any": (1.0, 1.0, "any"),
}
RRF_KS = (1, 10, 30, 60, 100, 200)
KEYWORD_WEIGHTS = (0.25, 0.5, 1.0, 2.0)

SEARCH_SQL = """
SELECT complaint_id, keyword_rank, semantic_rank
FROM search.hybrid_search(
    query_text => %(q)s, query_embedding => %(e)s, match_count => %(n)s,
    candidate_count => %(c)s, keyword_weight => %(kw)s, semantic_weight => %(sw)s,
    rrf_k => %(rrf_k)s, date_from => %(date_from)s, date_to => %(date_to)s,
    keyword_match => %(match)s
)
"""


@dataclass(frozen=True)
class LabelledQuery:
    id: str
    text: str
    product_family: str
    issue: str
    sub_issue: str


def load_queries() -> tuple[date, date, list[LabelledQuery]]:
    with open(common.QUERIES_FILE, "rb") as f:
        data = tomllib.load(f)
    queries = [LabelledQuery(**q) for q in data["query"]]
    ids = [q.id for q in queries]
    if len(set(ids)) != len(ids):
        raise ValueError("duplicate query ids in eval_queries.toml")
    return data["window"]["date_from"], data["window"]["date_to"], queries


def grades_for(q: LabelledQuery, labels: list[tuple[int, str, str, str]]) -> dict[int, int]:
    """Graded relevance of every complaint in the window for one query."""
    out = {}
    for complaint_id, family, issue, sub_issue in labels:
        if family != q.product_family or issue != q.issue:
            continue
        out[complaint_id] = 2 if sub_issue == q.sub_issue else 1
    return out


def fetch_labels(conn: psycopg.Connection, start: date, end: date) -> list[tuple]:
    return conn.execute(
        """
        SELECT f.complaint_id, f.product_family, s.issue, coalesce(s.sub_issue, '')
        FROM search.complaint_facets AS f
        INNER JOIN staging.stg_cfpb__complaints AS s ON f.complaint_id = s.complaint_id
        WHERE f.date_received BETWEEN %s AND %s
        """,
        (start, end),
    ).fetchall()


def score(ranked: list[int], grades: dict[int, int]) -> dict[str, float]:
    exact = {d for d, g in grades.items() if g == 2}
    return {
        "ndcg@10": metrics.ndcg_at_k(ranked, grades, K),
        "recall@10": metrics.capped_recall_at_k(ranked, exact, K),
        "recall@100": metrics.capped_recall_at_k(ranked, exact, DEEP_K),
        "mrr@10": metrics.reciprocal_rank(ranked, exact, K),
        "returned": float(len(ranked)),
    }


def run(conn: psycopg.Connection, encoder: Any) -> dict[str, Any]:
    start, end, queries = load_queries()
    labels = fetch_labels(conn, start, end)
    log.info("%s labelled complaints in %s..%s", len(labels), start, end)
    conn.execute("SELECT set_config('statement_timeout', '0', false)")

    per_query: list[dict[str, Any]] = []
    latencies: dict[str, list[float]] = {name: [] for name in SYSTEMS}
    sweep_scores: dict[str, list[float]] = {}
    fusion_mismatches = 0

    for i, q in enumerate(queries, start=1):
        grades = grades_for(q, labels)
        exact = sum(1 for g in grades.values() if g == 2)
        if exact == 0:
            raise ValueError(f"{q.id}: no complaint in the window carries its label")
        embedding = encoder.encode_query(q.text)
        row: dict[str, Any] = {"id": q.id, "relevant": exact, "partial": len(grades) - exact}
        for name, (kw, sw, match) in SYSTEMS.items():
            hybrid = kw > 0 and sw > 0
            args = {
                "q": q.text,
                "e": embedding,
                "c": CANDIDATES,
                "kw": kw,
                "sw": sw,
                "rrf_k": 60,
                "date_from": start,
                "date_to": end,
                "match": match,
                # Hybrid returns every fused complaint (up to 2 x candidates) for the sweep.
                "n": 2 * CANDIDATES if hybrid else DEEP_K,
            }
            with common.Timer() as t:
                rows = conn.execute(SEARCH_SQL, args).fetchall()
            latencies[name].append(t.seconds * 1000)
            ranked = [r[0] for r in rows][:DEEP_K]
            row[name] = score(ranked, grades)

            if hybrid:
                arm_rows = [(r[0], r[1], r[2]) for r in rows]
                if metrics.rrf_fuse(arm_rows, 60, 1.0, 1.0, DEEP_K) != ranked:
                    fusion_mismatches += 1
                for rrf_k in RRF_KS:
                    for w in KEYWORD_WEIGHTS:
                        fused = metrics.rrf_fuse(arm_rows, rrf_k, w, 1.0, K)
                        key = f"{match}|{rrf_k}|{w}"
                        sweep_scores.setdefault(key, []).append(metrics.ndcg_at_k(fused, grades, K))
        per_query.append(row)
        log.info(
            "[%s/%s] %s: ndcg@10 kw_all %.2f kw_any %.2f sem %.2f hyb_all %.2f hyb_any %.2f",
            i,
            len(queries),
            q.id,
            *(row[n]["ndcg@10"] for n in SYSTEMS),
        )

    if fusion_mismatches:
        raise RuntimeError(f"Python RRF disagreed with SQL on {fusion_mismatches} queries")

    summary = {}
    for name in SYSTEMS:
        vals = {m: [r[name][m] for r in per_query] for m in per_query[0][name]}
        summary[name] = {m: round(float(np.mean(v)), 4) for m, v in vals.items()}
        summary[name]["zero_results"] = sum(1 for r in per_query if r[name]["returned"] == 0)
        summary[name]["p50_ms"] = round(metrics.percentile(latencies[name], 50), 1)
        summary[name]["p95_ms"] = round(metrics.percentile(latencies[name], 95), 1)

    def ndcgs(name: str) -> list[float]:
        return [r[name]["ndcg@10"] for r in per_query]

    comparisons = {}
    for a, b in (
        ("hybrid_any", "semantic"),
        ("hybrid_all", "semantic"),
        ("hybrid_any", "hybrid_all"),
        ("keyword_any", "keyword_all"),
        ("semantic", "keyword_any"),
    ):
        mean, lo, hi = metrics.paired_bootstrap(ndcgs(a), ndcgs(b))
        wins = sum(1 for x, y in zip(ndcgs(a), ndcgs(b), strict=True) if x > y)
        losses = sum(1 for x, y in zip(ndcgs(a), ndcgs(b), strict=True) if x < y)
        comparisons[f"{a} vs {b}"] = {
            "mean_diff": round(mean, 4),
            "ci95": [round(lo, 4), round(hi, 4)],
            "wins": wins,
            "losses": losses,
            "ties": len(per_query) - wins - losses,
        }

    sweep = [
        {
            "keyword_match": key.split("|")[0],
            "rrf_k": int(key.split("|")[1]),
            "keyword_weight": float(key.split("|")[2]),
            "ndcg@10": round(float(np.mean(v)), 4),
        }
        for key, v in sweep_scores.items()
    ]

    results = {
        "environment": common.environment(conn),
        "window": {"date_from": start, "date_to": end, "labelled_complaints": len(labels)},
        "queries": len(queries),
        "candidates_per_arm": CANDIDATES,
        "model": getattr(encoder, "name", None),
        "summary": summary,
        "comparisons": comparisons,
        "rrf_sweep": sweep,
        "per_query": per_query,
    }
    common.save_results("search_quality", results)
    return results
