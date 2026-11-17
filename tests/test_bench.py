"""Benchmark harness pieces that must be right for the numbers to mean anything."""

from __future__ import annotations

import math
import tomllib

import numpy as np
import pytest

from complaint_warehouse.bench import common, knn, metrics, report
from complaint_warehouse.bench.index_study import IndexSpec, default_specs
from complaint_warehouse.bench.quality import LabelledQuery, grades_for, load_queries

# --- metrics --------------------------------------------------------------------------


def test_ann_recall_counts_overlap_with_the_exact_top_k():
    assert metrics.ann_recall([1, 2, 3, 9], [1, 2, 3, 4], 4) == 0.75
    assert metrics.ann_recall([4, 3, 2, 1], [1, 2, 3, 4], 4) == 1.0  # order does not matter
    assert metrics.ann_recall([], [1, 2], 2) == 0.0


def test_ndcg_is_one_for_the_ideal_order_and_penalises_missed_documents():
    grades = {1: 2, 2: 1, 3: 1}
    assert metrics.ndcg_at_k([1, 2, 3], grades, 10) == pytest.approx(1.0)
    worse = metrics.ndcg_at_k([3, 2, 1], grades, 10)
    assert 0 < worse < 1
    # Hand-computed: DCG = 3/log2(2) ; IDCG adds the two grade-1 docs it did not find.
    expected = 3.0 / (3.0 + 1 / math.log2(3) + 1 / math.log2(4))
    assert metrics.ndcg_at_k([1, 99], grades, 10) == pytest.approx(expected)
    assert metrics.ndcg_at_k([5, 6], {}, 10) == 0.0


def test_capped_recall_is_precision_with_many_relevant_and_recall_with_few():
    many = set(range(100))
    assert metrics.capped_recall_at_k([1, 2, 500, 3], many, 4) == 0.75
    few = {7, 8}
    assert metrics.capped_recall_at_k([7, 1, 2, 3], few, 4) == 0.5
    assert metrics.capped_recall_at_k([1], set(), 4) == 0.0


def test_reciprocal_rank():
    assert metrics.reciprocal_rank([5, 6, 7], {7}, 10) == pytest.approx(1 / 3)
    assert metrics.reciprocal_rank([5, 6, 7], {7}, 2) == 0.0


def test_paired_bootstrap_brackets_the_mean_difference():
    rng = np.random.default_rng(1)
    a = rng.uniform(0.4, 0.6, 50)
    b = a - 0.1
    mean, lo, hi = metrics.paired_bootstrap(a, b, n_resamples=2000)
    assert mean == pytest.approx(0.1)
    assert lo == pytest.approx(0.1) and hi == pytest.approx(0.1)  # constant difference
    mean, lo, hi = metrics.paired_bootstrap(a, a[::-1], n_resamples=2000)
    assert lo < mean < hi


def test_rrf_fuse_matches_the_sql_formula_and_tie_break():
    rows = [(10, 1, None), (20, None, 1), (30, 2, 2), (5, None, 1)]
    # 30 appears in both arms: 1/62 + 1/62 beats 1/61 alone.
    assert metrics.rrf_fuse(rows, 60, 1.0, 1.0, 10) == [30, 5, 10, 20]
    # Ties (10, 20 and 5 each score 1/61) break by id ascending, as the SQL does.
    assert metrics.rrf_fuse(rows, 60, 1.0, 1.0, 2) == [30, 5]
    # A heavier keyword weight lifts the keyword-only hit above semantic-only ones.
    assert metrics.rrf_fuse(rows, 60, 2.0, 1.0, 2) == [30, 10]


# --- exact kNN --------------------------------------------------------------------------


def test_streaming_topk_merge_equals_brute_force():
    rng = np.random.default_rng(0)
    x = knn.normalise(rng.normal(size=(1000, 16)))
    q = knn.normalise(rng.normal(size=(5, 16)))
    ids = np.arange(1000, 2000, dtype=np.int64)
    best_ids = np.empty((5, 0), dtype=np.int64)
    best_sims = np.empty((5, 0), dtype=np.float32)
    for start in range(0, 1000, 64):  # uneven last batch on purpose
        part = slice(start, start + 64)
        best_ids, best_sims = knn.merge_topk(best_ids, best_sims, ids[part], q @ x[part].T, 10)
    got_ids, got_dist = knn.sort_topk(best_ids, best_sims)

    sims = q @ x.T
    want = ids[np.argsort(-sims, axis=1, kind="stable")[:, :10]]
    np.testing.assert_array_equal(got_ids, want)
    assert np.all(np.diff(got_dist, axis=1) >= 0)


# --- index specs --------------------------------------------------------------------------


def test_index_specs_generate_matching_index_and_query_expressions():
    half = IndexSpec("h", "hnsw", "halfvec", {"m": 16, "ef_construction": 64})
    assert "USING hnsw ((embedding::halfvec(768)) halfvec_cosine_ops)" in half.ddl("t")
    assert "WITH (m = 16, ef_construction = 64)" in half.ddl("t")
    # The query must use the same expression as the index or the planner cannot use it.
    assert "ORDER BY embedding::halfvec(768) <=> %(q)s::halfvec(768)" in half.query("t")
    vec = IndexSpec("v", "ivfflat", "vector", {"lists": 500})
    assert "USING ivfflat (embedding vector_cosine_ops) WITH (lists = 500)" in vec.ddl("t")
    assert IndexSpec("e", "exact").sweep() == [(None, None)]


def test_ivfflat_lists_follow_pgvector_guidance():
    lists = {s.name: s.params.get("lists") for s in default_specs(500_000)}
    assert lists["ivfflat_vec_l500"] == 500
    big = {s.name for s in default_specs(5_600_000)}
    assert "ivfflat_vec_l2366" in big  # sqrt(rows) above 1M rows


# --- labelled queries --------------------------------------------------------------------


def test_eval_queries_file_is_well_formed():
    start, end, queries = load_queries()
    assert start < end
    assert len(queries) >= 50
    assert all(q.text and q.product_family and q.issue for q in queries)
    with open(common.QUERIES_FILE, "rb") as f:
        raw = tomllib.load(f)
    assert len(raw["query"]) == len(queries)


def test_grades_distinguish_sub_issue_from_issue():
    q = LabelledQuery("x", "t", "Debt collection", "Communication tactics", "Frequent calls")
    labels = [
        (1, "Debt collection", "Communication tactics", "Frequent calls"),
        (2, "Debt collection", "Communication tactics", "Called at work"),
        (3, "Debt collection", "Other issue", "Frequent calls"),
        (4, "Mortgages", "Communication tactics", "Frequent calls"),
    ]
    assert grades_for(q, labels) == {1: 2, 2: 1}
    issue_only = LabelledQuery("y", "t", "Mortgages", "Communication tactics", "")
    assert grades_for(issue_only, [(5, "Mortgages", "Communication tactics", "")]) == {5: 2}


# --- report -----------------------------------------------------------------------------


def test_markdown_table_escapes_pipes_and_formats_numbers():
    md = report.table(["a", "b"], [("x|y", 0.12345), (1234, None)])
    assert "x\\|y" in md and "0.1235" in md and "1,234" in md and "| - |" in md
