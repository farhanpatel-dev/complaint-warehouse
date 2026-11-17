"""Shared helpers for the benchmark commands: connections, environment capture, result files."""

from __future__ import annotations

import json
import os
import platform
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psycopg

from complaint_warehouse import __version__
from complaint_warehouse.config import get_settings

BENCH_DIR = Path(os.getenv("BENCH_DIR", "bench"))
RESULTS_DIR = BENCH_DIR / "results"
EXPLAIN_DIR = RESULTS_DIR / "explain"
QUERIES_FILE = BENCH_DIR / "queries" / "eval_queries.toml"
ANALYTICS_DIR = Path(os.getenv("ANALYTICS_SQL_DIR", "sql/analytics"))

# Settings that change benchmark results; recorded with every run.
PG_SETTINGS = (
    "shared_buffers",
    "effective_cache_size",
    "work_mem",
    "maintenance_work_mem",
    "max_parallel_workers_per_gather",
    "max_parallel_maintenance_workers",
    "random_page_cost",
    "jit",
)


def bench_connect() -> psycopg.Connection:
    """Autocommit connection with pgvector registered and no statement timeout."""
    from pgvector.psycopg import register_vector

    conn = psycopg.connect(get_settings().database_url, autocommit=True)
    register_vector(conn)
    return conn


def environment(conn: psycopg.Connection) -> dict[str, Any]:
    """What the numbers were measured on, so they can be compared or reproduced."""
    pg = conn.execute("SHOW server_version").fetchone()[0]
    vec = conn.execute("SELECT extversion FROM pg_extension WHERE extname = 'vector'").fetchone()
    settings = {name: conn.execute(f"SHOW {name}").fetchone()[0] for name in PG_SETTINGS}
    mem_total = None
    try:
        with open("/proc/meminfo") as f:
            mem_total = next(line.split(":")[1].strip() for line in f if "MemTotal" in line)
    except (OSError, StopIteration):
        pass
    return {
        "measured_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "package_version": __version__,
        "postgres": pg,
        "pgvector": vec[0] if vec else None,
        "settings": settings,
        "client": {
            "python": platform.python_version(),
            "cpus": os.cpu_count(),
            "mem_total": mem_total,
            "machine": platform.machine(),
        },
    }


def load_results(name: str) -> dict[str, Any]:
    path = RESULTS_DIR / f"{name}.json"
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {}


def save_results(name: str, data: dict[str, Any]) -> Path:
    """Write atomically, so a run killed mid-write keeps the previous complete file."""
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    path = RESULTS_DIR / f"{name}.json"
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2, default=str) + "\n", encoding="utf-8")
    tmp.replace(path)
    return path


def save_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text.rstrip() + "\n", encoding="utf-8")


def explain_text(conn: psycopg.Connection, query: str, params: Any = None) -> str:
    rows = conn.execute(f"EXPLAIN (ANALYZE, BUFFERS) {query}", params).fetchall()
    return "\n".join(r[0] for r in rows)


class Timer:
    def __enter__(self) -> Timer:
        self.start = time.perf_counter()
        return self

    def __exit__(self, *exc: object) -> None:
        self.seconds = time.perf_counter() - self.start
