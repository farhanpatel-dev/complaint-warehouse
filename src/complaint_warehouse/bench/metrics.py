"""Retrieval metrics used by the benchmarks. Pure functions, no database.

Two different questions are measured, with two different notions of "correct":

* ANN recall (index study): did the approximate index return the same neighbours as an
  exact scan? Ground truth is the exact top-k, so recall@k = |ANN top-k ∩ exact top-k| / k.
* Retrieval quality (search evaluation): did the search return complaints a person would
  call relevant? Ground truth is graded labels; I report nDCG@k, capped recall and MRR.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence

import numpy as np


def ann_recall(found: Sequence[int], exact: Sequence[int], k: int) -> float:
    """Share of the exact top-k that the approximate search also returned in its top-k."""
    truth = set(exact[:k])
    if not truth:
        return 1.0
    return len(truth.intersection(found[:k])) / len(truth)


def dcg(gains: Iterable[float]) -> float:
    """Discounted cumulative gain with exponential gain, (2^g - 1) / log2(rank + 1)."""
    return sum((2.0**g - 1.0) / math.log2(i + 2) for i, g in enumerate(gains))


def ndcg_at_k(ranked: Sequence[int], grades: Mapping[int, int], k: int) -> float:
    """nDCG@k against graded labels; unlabelled documents have grade 0.

    The ideal ordering is computed from all labelled documents, not only the ones that were
    retrieved, so a method is penalised for relevant complaints it failed to find.
    """
    ideal = dcg(sorted(grades.values(), reverse=True)[:k])
    if ideal == 0:
        return 0.0
    return dcg(grades.get(doc, 0) for doc in ranked[:k]) / ideal


def capped_recall_at_k(ranked: Sequence[int], relevant: set[int], k: int) -> float:
    """Relevant documents in the top-k divided by min(k, |relevant|).

    Plain recall@10 is meaningless when a query has 500 relevant complaints (the best
    possible value is 0.02). Capping the denominator, as BEIR does, makes 1.0 reachable:
    with many relevant documents it equals precision@k, with few it is ordinary recall.
    """
    if not relevant:
        return 0.0
    hits = sum(1 for doc in ranked[:k] if doc in relevant)
    return hits / min(k, len(relevant))


def reciprocal_rank(ranked: Sequence[int], relevant: set[int], k: int) -> float:
    """1 / rank of the first relevant document within the top-k, else 0."""
    for i, doc in enumerate(ranked[:k], start=1):
        if doc in relevant:
            return 1.0 / i
    return 0.0


def percentile(values: Sequence[float], p: float) -> float:
    """Linear-interpolated percentile, e.g. p=95. Returns nan for no values."""
    if not values:
        return float("nan")
    return float(np.percentile(np.asarray(values, dtype=float), p))


def paired_bootstrap(
    a: Sequence[float], b: Sequence[float], n_resamples: int = 10_000, seed: int = 0
) -> tuple[float, float, float]:
    """Mean of (a - b) over queries and its 95% bootstrap confidence interval.

    Paired, because both methods answer the same queries: resampling queries keeps each
    query's pair together, so per-query difficulty cancels out.
    """
    diff = np.asarray(a, dtype=float) - np.asarray(b, dtype=float)
    if diff.size == 0:
        return float("nan"), float("nan"), float("nan")
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, diff.size, size=(n_resamples, diff.size))
    means = diff[idx].mean(axis=1)
    lo, hi = np.percentile(means, [2.5, 97.5])
    return float(diff.mean()), float(lo), float(hi)


def rrf_fuse(
    rows: Iterable[tuple[int, int | None, int | None]],
    rrf_k: int,
    keyword_weight: float,
    semantic_weight: float,
    limit: int,
) -> list[int]:
    """Reciprocal Rank Fusion of (doc, keyword_rank, semantic_rank) rows.

    Mirrors search.hybrid_search exactly, tie-break included (score desc, then id asc), so
    a weight or rrf_k sweep can be computed from one call's arm ranks instead of one SQL
    call per setting. The evaluation checks that this reproduces the SQL ordering.
    """
    scored = []
    for doc, kr, sr in rows:
        score = 0.0
        if kr is not None:
            score += keyword_weight / (rrf_k + kr)
        if sr is not None:
            score += semantic_weight / (rrf_k + sr)
        scored.append((-score, doc))
    scored.sort()
    return [doc for _, doc in scored[:limit]]
