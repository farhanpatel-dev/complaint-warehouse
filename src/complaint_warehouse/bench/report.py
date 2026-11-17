"""Render benchmark result JSON as Markdown tables (bench/results/*.md).

Every number in the Markdown comes from the JSON written by the run; nothing is typed in
by hand, so the reports can be regenerated at any time with `bench report`.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from complaint_warehouse.bench import common


def table(headers: Sequence[str], rows: Sequence[Sequence[Any]]) -> str:
    def cell(v: Any) -> str:
        if v is None:
            return "-"
        if isinstance(v, float):
            return f"{v:,.4g}" if abs(v) < 1 else f"{v:,.1f}"
        if isinstance(v, int):
            return f"{v:,}"
        return str(v).replace("|", "\\|")

    lines = ["| " + " | ".join(headers) + " |", "|" + "---|" * len(headers)]
    lines += ["| " + " | ".join(cell(v) for v in row) + " |" for row in rows]
    return "\n".join(lines)


def env_block(env: dict[str, Any]) -> str:
    s = env.get("settings", {})
    c = env.get("client", {})
    return (
        f"Measured {env.get('measured_at')} on PostgreSQL {env.get('postgres')}, "
        f"pgvector {env.get('pgvector')}; shared_buffers {s.get('shared_buffers')}, "
        f"work_mem {s.get('work_mem')}, max_parallel_workers_per_gather "
        f"{s.get('max_parallel_workers_per_gather')}, random_page_cost "
        f"{s.get('random_page_cost', '4 (default)')}, jit {s.get('jit')}. "
        f"Client container: {c.get('cpus')} CPUs, {c.get('mem_total')} RAM."
    )


def index_study_md(r: dict[str, Any]) -> str:
    ds = r["dataset"]
    out = [
        "# Vector index study",
        "",
        f"{ds['rows']:,} chunks from {ds['complaints']:,} complaints "
        f"({ds['sample_pct']}% hash sample of `{ds['source_table']}`, {ds['table_size']}). "
        f"{ds['queries']} queries ({ds['queries_by_source']['eval']} written evaluation "
        f"queries, {ds['queries_by_source']['cfpb_label']} CFPB issue/sub-issue names), "
        f"embedded with `{ds['model']}`. Ground truth: exact top-100 by cosine distance, "
        f"computed client-side in {ds['ground_truth_seconds']} s.",
        "",
    ]
    if "environment" in r:
        out += [env_block(r["environment"]), ""]
    indexes = r.get("indexes", {})
    builds = [
        (
            name,
            res["spec"]["method"],
            res["spec"]["precision"],
            ", ".join(f"{k}={v}" for k, v in res["spec"]["params"].items()) or "-",
            res["build"]["seconds"],
            res["build"]["size"],
            "; ".join(res["build"]["notices"]) or "-",
        )
        for name, res in indexes.items()
        if "build" in res
    ]
    if builds:
        out += [
            "## Build",
            "",
            table(
                ["index", "method", "type", "params", "build s", "size", "notices"],
                builds,
            ),
            "",
        ]
    for limit in (10, 100):
        rows = []
        for name, res in indexes.items():
            for p in res["points"]:
                if p["limit"] != limit:
                    continue
                setting = f"{p['setting'].split('.')[1]}={p['value']}" if p["setting"] else "-"
                if not p.get("planner_chose_index", True):
                    setting += " (forced)"
                rows.append((name, setting, p["recall"], p["p50_ms"], p["p95_ms"], p["queries"]))
        if rows:
            out += [
                f"## Recall@{limit} and latency (LIMIT {limit}, warm cache)",
                "",
                table(
                    ["index", "setting", f"recall@{limit}", "p50 ms", "p95 ms", "queries"],
                    rows,
                ),
                "",
            ]
    gin = r.get("gin")
    if gin:
        rows = []
        for label in ("no_index", "gin"):
            for match, m in gin[label].items():
                rows.append(
                    (
                        "GIN" if label == "gin" else "none (seq scan)",
                        match,
                        m["p50_ms"],
                        m["p95_ms"],
                        m["median_matching_chunks"],
                        m["queries_with_no_match"],
                    )
                )
        out += [
            "## Full-text keyword search (GIN on tsv)",
            "",
            f"{gin['queries']} evaluation queries, top 100 by `ts_rank_cd`. GIN build "
            f"{gin['build']['seconds']} s, {gin['build']['size']}.",
            "",
            table(
                ["index", "match", "p50 ms", "p95 ms", "median matching chunks", "no match"],
                rows,
            ),
            "",
        ]
    out += [
        "(forced): the planner chose a sequential scan at this setting; the index was",
        "measured with `enable_seqscan = off`.",
        "",
        "Query plans: `bench/results/explain/index_*.txt`, `gin_*.txt`.",
        "",
    ]
    return "\n".join(out)


def quality_md(r: dict[str, Any]) -> str:
    w = r["window"]
    out = [
        "# Search quality: keyword vs semantic vs hybrid",
        "",
        f"{r['queries']} labelled queries over complaints received {w['date_from']} to "
        f"{w['date_to']} ({w['labelled_complaints']:,} complaints with narratives), "
        f"{r['candidates_per_arm']} candidate chunks per arm, model `{r['model']}`. "
        "Relevance from CFPB issue/sub-issue labels: grade 2 exact sub-issue, grade 1 same "
        "issue. Recall is capped recall (hits / min(k, relevant)) on grade-2 complaints.",
        "",
        env_block(r["environment"]),
        "",
        "## Summary",
        "",
        table(
            [
                "system",
                "nDCG@10",
                "recall@10",
                "recall@100",
                "MRR@10",
                "zero results",
                "p50 ms",
                "p95 ms",
            ],
            [
                (
                    name,
                    s["ndcg@10"],
                    s["recall@10"],
                    s["recall@100"],
                    s["mrr@10"],
                    s["zero_results"],
                    s["p50_ms"],
                    s["p95_ms"],
                )
                for name, s in r["summary"].items()
            ],
        ),
        "",
        "Latency caveat: for each query the systems run in the order above, so the hybrid "
        "calls find the 2019 vectors already cached by the semantic call. Compare quality "
        "across systems; read latency within one system, or from the index study.",
        "",
        "## Paired comparisons (nDCG@10, 10,000 bootstrap resamples of the queries)",
        "",
        table(
            ["comparison", "mean diff", "95% CI", "wins", "losses", "ties"],
            [
                (
                    k,
                    v["mean_diff"],
                    f"[{v['ci95'][0]:+.3f}, {v['ci95'][1]:+.3f}]",
                    v["wins"],
                    v["losses"],
                    v["ties"],
                )
                for k, v in r["comparisons"].items()
            ],
        ),
        "",
        "## RRF sensitivity (nDCG@10; semantic weight 1)",
        "",
    ]
    for match in ("all", "any"):
        cells = [s for s in r["rrf_sweep"] if s["keyword_match"] == match]
        ks = sorted({s["rrf_k"] for s in cells})
        ws = sorted({s["keyword_weight"] for s in cells})
        lookup = {(s["rrf_k"], s["keyword_weight"]): s["ndcg@10"] for s in cells}
        out += [
            f"keyword_match = `{match}`:",
            "",
            table(
                ["rrf_k \\ keyword weight", *[str(w) for w in ws]],
                [(k, *[lookup[(k, w)] for w in ws]) for k in ks],
            ),
            "",
        ]
    out += [
        "## Per query (nDCG@10)",
        "",
        table(
            ["query", "relevant", *r["summary"].keys()],
            [
                (q["id"], q["relevant"], *[q[name]["ndcg@10"] for name in r["summary"]])
                for q in r["per_query"]
            ],
        ),
        "",
    ]
    return "\n".join(out)


def analytics_md(r: dict[str, Any]) -> str:
    out = [
        "# Analytics SQL library",
        "",
        f"Each query run {r['runs']} times with `EXPLAIN (ANALYZE, TIMING OFF)`; server-side "
        "execution time without per-node timing overhead. Median excludes the first run. "
        "The saved plans include per-node timing.",
        "",
        env_block(r["environment"]),
        "",
        table(
            ["query", "rows", "first run ms", "median ms", "planning ms"],
            [
                (name, q["rows"], q["first_run_ms"], q["median_ms"], q["planning_ms"])
                for name, q in r["queries"].items()
            ],
        ),
        "",
    ]
    for name, q in r["queries"].items():
        if not q["sample"]:
            continue
        cols = list(q["sample"][0].keys())
        out += [
            f"### {name}",
            "",
            f"`{q['file']}`, first {len(q['sample'])} rows:",
            "",
            table(cols, [[row[c] for c in cols] for row in q["sample"]]),
            "",
        ]
    out += ["Query plans: `bench/results/explain/analytics_*.txt`.", ""]
    return "\n".join(out)


RENDERERS = {
    "index_study": index_study_md,
    "search_quality": quality_md,
    "analytics": analytics_md,
}


def render_all() -> list[str]:
    written = []
    for name, render in RENDERERS.items():
        data = common.load_results(name)
        if not data or (name == "index_study" and "dataset" not in data):
            continue
        path = common.RESULTS_DIR / f"{name}.md"
        common.save_text(path, render(data))
        written.append(path.as_posix())
    return written
