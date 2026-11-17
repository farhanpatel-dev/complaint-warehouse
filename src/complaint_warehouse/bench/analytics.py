"""Time the analytics SQL library (sql/analytics/*.sql) with EXPLAIN ANALYZE.

Each query runs `runs` times; the first run is reported separately as the cold-ish run
and the median of the rest as the warm time. Execution time comes from the server's own
EXPLAIN ANALYZE with TIMING OFF, which skips the per-node clock calls (the total is all
I need here) and excludes network and client time. The text plan of the last run is
saved next to the results, and a few result rows are kept to show what the query returns.
"""

from __future__ import annotations

import logging
import statistics
from pathlib import Path
from typing import Any

import psycopg

from complaint_warehouse.bench import common

log = logging.getLogger(__name__)


def statement(path: Path) -> str:
    return path.read_text(encoding="utf-8").strip().rstrip(";")


def run(conn: psycopg.Connection, runs: int = 5, sample_rows: int = 5) -> dict[str, Any]:
    files = sorted(common.ANALYTICS_DIR.glob("*.sql"))
    if not files:
        raise SystemExit(f"no .sql files in {common.ANALYTICS_DIR}")
    out: dict[str, Any] = {"environment": common.environment(conn), "runs": runs, "queries": {}}
    for path in files:
        sql_text = statement(path)
        exec_ms, plan_ms = [], []
        plan: dict[str, Any] = {}
        for _ in range(runs):
            plan = conn.execute(
                f"EXPLAIN (ANALYZE, TIMING OFF, BUFFERS, FORMAT JSON) {sql_text}"
            ).fetchone()[0][0]
            exec_ms.append(plan["Execution Time"])
            plan_ms.append(plan["Planning Time"])
        cur = conn.execute(f"SELECT * FROM ({sql_text}) AS q LIMIT {int(sample_rows)}")
        columns = [d.name for d in cur.description or []]
        sample = [dict(zip(columns, row, strict=True)) for row in cur.fetchall()]
        common.save_text(
            common.EXPLAIN_DIR / f"analytics_{path.stem}.txt", common.explain_text(conn, sql_text)
        )
        warm = exec_ms[1:] or exec_ms
        out["queries"][path.stem] = {
            "file": path.as_posix(),
            "rows": plan["Plan"]["Actual Rows"],
            "first_run_ms": round(exec_ms[0], 1),
            "median_ms": round(statistics.median(warm), 1),
            "planning_ms": round(statistics.median(plan_ms), 1),
            "shared_hit_blocks": plan["Plan"].get("Shared Hit Blocks"),
            "shared_read_blocks": plan["Plan"].get("Shared Read Blocks"),
            "sample": sample,
        }
        log.info(
            "%s: %s rows, median %.1f ms",
            path.stem,
            plan["Plan"]["Actual Rows"],
            out["queries"][path.stem]["median_ms"],
        )
    common.save_results("analytics", out)
    return out
