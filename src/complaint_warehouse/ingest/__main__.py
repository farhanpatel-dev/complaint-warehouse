"""Command line entry point: `python -m complaint_warehouse.ingest <command>`.

Examples:
    python -m complaint_warehouse.ingest api                          # incremental from watermark
    python -m complaint_warehouse.ingest api --start 2026-10-01 --end 2026-10-07
    python -m complaint_warehouse.ingest bulk --start 2023-01-01      # backfill from bulk CSV
    python -m complaint_warehouse.ingest narratives --start 2025-01-01
    python -m complaint_warehouse.ingest narratives --archives 20,21
    python -m complaint_warehouse.ingest status
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import UTC, date, datetime
from pathlib import Path

from complaint_warehouse.config import get_settings
from complaint_warehouse.db import connect
from complaint_warehouse.ingest import pipeline
from complaint_warehouse.ingest.loader import ensure_schema
from complaint_warehouse.ingest.sources.api import CfpbApiClient
from complaint_warehouse.ingest.sources.archives import select_archives

log = logging.getLogger("complaint_warehouse.ingest")


def _date(value: str) -> date:
    return date.fromisoformat(value)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m complaint_warehouse.ingest")
    sub = parser.add_subparsers(dest="command", required=True)

    api = sub.add_parser("api", help="incremental load from the CFPB search API")
    api.add_argument(
        "--start", type=_date, help="first date_received (default: watermark minus lookback)"
    )
    api.add_argument("--end", type=_date, help="last date_received (default: today, UTC)")
    api.add_argument("--lookback-days", type=int)
    api.add_argument("--window-days", type=int)

    bulk = sub.add_parser("bulk", help="backfill from complaints.csv.zip")
    bulk.add_argument("--file", type=Path, help="use a local zip instead of downloading")
    bulk.add_argument("--start", type=_date)
    bulk.add_argument("--end", type=_date)

    narr = sub.add_parser("narratives", help="load consumer narratives from the CFPB archive")
    narr.add_argument("--archives", help="comma-separated archive numbers (1-21)")
    narr.add_argument("--file", type=Path, help="load one local archive zip")
    narr.add_argument("--start", type=_date)
    narr.add_argument("--end", type=_date)
    narr.add_argument("--force", action="store_true", help="reload files already loaded")

    sub.add_parser("status", help="show watermark, recent runs and row counts")
    return parser


def cmd_api(args: argparse.Namespace) -> None:
    s = get_settings()
    with connect() as conn:
        ensure_schema(conn)
        conn.commit()
        start, end = pipeline.plan_incremental_range(
            watermark=pipeline.current_watermark(conn),
            lookback_days=(
                args.lookback_days if args.lookback_days is not None else s.ingest_lookback_days
            ),
            default_start=s.ingest_start_date,
            today=datetime.now(UTC).date(),
            start=args.start,
            end=args.end,
        )
        log.info("api load %s..%s", start, end)
        with CfpbApiClient(
            s.cfpb_api_url,
            s.cfpb_user_agent,
            page_size=s.api_page_size,
            timeout=s.http_timeout_seconds,
            max_retries=s.http_max_retries,
        ) as client:
            run = pipeline.run_api(
                conn,
                client,
                start=start,
                end=end,
                window_days=args.window_days or s.ingest_window_days,
                batch_size=s.ingest_batch_size,
            )
    _report(run)


def cmd_bulk(args: argparse.Namespace) -> None:
    s = get_settings()
    path = args.file or pipeline.fetch_bulk(s)
    with connect() as conn:
        ensure_schema(conn)
        conn.commit()
        run = pipeline.run_bulk(
            conn, path, start=args.start, end=args.end, batch_size=s.ingest_batch_size
        )
    _report(run)


def cmd_narratives(args: argparse.Namespace) -> None:
    s = get_settings()
    with connect() as conn:
        ensure_schema(conn)
        conn.commit()
        if args.file:
            runs = [
                r
                for r in [
                    pipeline.load_narrative_file(
                        conn,
                        args.file,
                        start=args.start,
                        end=args.end,
                        batch_size=s.ingest_batch_size,
                        force=args.force,
                    )
                ]
                if r
            ]
        else:
            numbers = {int(n) for n in args.archives.split(",")} if args.archives else None
            archives = select_archives(args.start, args.end, numbers)
            log.info("archives selected: %s", [a.number for a in archives])
            runs = pipeline.run_narratives(
                conn, archives, s, start=args.start, end=args.end, force=args.force
            )
    for run in runs:
        _report(run)


def cmd_status(_: argparse.Namespace) -> None:
    with connect() as conn:
        ensure_schema(conn)
        conn.commit()
        print(f"watermark[{pipeline.WATERMARK_KEY}]: {pipeline.current_watermark(conn)}")
        for table in ("raw.complaints", "raw.complaint_narratives"):
            n, lo, hi = conn.execute(
                f"SELECT count(*), min(date_received), max(date_received) FROM {table}"
            ).fetchone()
            print(f"{table:28s} rows={n:>10,}  date_received {lo} .. {hi}")
        print("\nrecent runs:")
        rows = conn.execute(
            "SELECT load_id, source, status, started_at, finished_at - started_at, rows_read, "
            "rows_inserted, rows_updated, params FROM ops.load_runs ORDER BY load_id DESC LIMIT 10"
        ).fetchall()
        for r in rows:
            print(
                f"  #{r[0]:<4} {r[1]:10s} {r[2]:9s} {r[3]:%Y-%m-%d %H:%M} took={r[4]} "
                f"read={r[5]:,} ins={r[6]:,} upd={r[7]:,} {r[8]}"
            )


def _report(run: pipeline.LoadRun) -> None:
    st = run.stats
    print(
        f"load #{run.load_id}: read={st.read:,} inserted={st.inserted:,} "
        f"updated={st.updated:,} unchanged={st.unchanged:,}"
    )


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=get_settings().log_level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    {"api": cmd_api, "bulk": cmd_bulk, "narratives": cmd_narratives, "status": cmd_status}[
        args.command
    ](args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
