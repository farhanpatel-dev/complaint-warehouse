"""Benchmarks: `python -m complaint_warehouse.bench <command>` (or `make bench ARGS=...`).

Commands, in the order a full run needs them:
    prepare     sample chunks into bench.chunk_vectors, embed queries, exact ground truth
    index       build and sweep HNSW / IVFFlat (float32 and halfvec) on the sample
    gin         keyword search with and without a GIN index, AND vs OR matching
    quality     keyword vs semantic vs hybrid on the labelled queries (full corpus, 2019)
    analytics   time the sql/analytics library
    report      re-render bench/results/*.md from the JSON results

Results go to bench/results/ as JSON plus generated Markdown; query plans to
bench/results/explain/. Each command can be re-run on its own.
"""

from __future__ import annotations

import argparse
import logging
import sys

from complaint_warehouse.bench import common, report

log = logging.getLogger("complaint_warehouse.bench")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m complaint_warehouse.bench")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("prepare", help="sample, embed queries, compute exact ground truth")
    p.add_argument("--sample-pct", type=float, default=9.0, help="share of complaints (hash)")
    p.add_argument("--queries", type=int, default=200, help="total queries incl. eval ones")
    p.add_argument("--source-table", default="search.complaint_text")
    p.add_argument("--keep-table", action="store_true", help="reuse bench.chunk_vectors")

    i = sub.add_parser("index", help="build and sweep vector indexes on the sample")
    i.add_argument("--only", nargs="+", help="index names to run, e.g. hnsw_half_m16_efc64")
    i.add_argument("--force", action="store_true", help="re-measure indexes already in results")
    i.add_argument("--maintenance-work-mem", default="2GB")
    i.add_argument("--parallel-workers", type=int, default=4)
    i.add_argument("--exact-queries", type=int, default=20, help="queries for the exact scan")

    g = sub.add_parser("gin", help="full-text search with and without GIN")
    g.add_argument("--maintenance-work-mem", default="1GB")

    sub.add_parser("quality", help="keyword vs semantic vs hybrid on labelled queries")

    a = sub.add_parser("analytics", help="time the analytics SQL library")
    a.add_argument("--runs", type=int, default=5)

    sub.add_parser("report", help="render Markdown from the JSON results")
    return parser


def _encoder():  # type: ignore[no-untyped-def]
    from complaint_warehouse.config import get_settings
    from complaint_warehouse.embed.model import load_encoder

    return load_encoder(get_settings())


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    for noisy in ("httpx", "huggingface_hub", "urllib3", "filelock", "sentence_transformers"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    if args.command == "report":
        for path in report.render_all():
            log.info("wrote %s", path)
        return 0

    with common.bench_connect() as conn:
        if args.command == "prepare":
            from complaint_warehouse.bench import index_study

            info = index_study.prepare(
                conn,
                _encoder(),
                args.sample_pct,
                args.queries,
                args.source_table,
                args.keep_table,
            )
            log.info("prepared: %s", info)
        elif args.command == "index":
            from complaint_warehouse.bench import index_study

            index_study.run_study(
                conn,
                args.only,
                args.force,
                args.maintenance_work_mem,
                args.parallel_workers,
                args.exact_queries,
            )
        elif args.command == "gin":
            from complaint_warehouse.bench import index_study

            index_study.run_gin(conn, args.maintenance_work_mem)
        elif args.command == "quality":
            from complaint_warehouse.bench import quality

            quality.run(conn, _encoder())
        elif args.command == "analytics":
            from complaint_warehouse.bench import analytics

            analytics.run(conn, args.runs)
    for path in report.render_all():
        log.info("wrote %s", path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
