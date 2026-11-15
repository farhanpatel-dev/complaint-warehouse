"""Command line entry point: `python -m complaint_warehouse.embed <command>`.

Examples:
    python -m complaint_warehouse.embed run                  # embed everything pending
    python -m complaint_warehouse.embed run --limit 20000    # a sample, e.g. to time it
    python -m complaint_warehouse.embed status
    python -m complaint_warehouse.embed reset --yes          # drop vectors from other models
"""

from __future__ import annotations

import argparse
import logging
import sys

from complaint_warehouse.config import get_settings
from complaint_warehouse.db import connect
from complaint_warehouse.embed import job

log = logging.getLogger("complaint_warehouse.embed")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m complaint_warehouse.embed")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="embed pending chunks (resumes from the checkpoint)")
    run.add_argument("--limit", type=int, help="stop after embedding this many chunks")
    run.add_argument("--fetch-size", type=int, help="chunks per checkpointed batch")
    run.add_argument("--batch-size", type=int, help="texts per forward pass")
    run.add_argument("--device", help="auto, cpu, cuda or cuda:N")
    run.add_argument("--table", default=job.DEFAULT_TABLE)

    status = sub.add_parser("status", help="pending/embedded counts and checkpoints")
    status.add_argument("--table", default=job.DEFAULT_TABLE)

    reset = sub.add_parser("reset", help="clear vectors that the configured model did not make")
    reset.add_argument("--table", default=job.DEFAULT_TABLE)
    reset.add_argument("--all", action="store_true", help="clear every vector, current model too")
    reset.add_argument("--yes", action="store_true", help="required; this deletes vectors")
    return parser


def cmd_run(args: argparse.Namespace) -> None:
    from complaint_warehouse.embed.model import load_encoder

    s = get_settings()
    overrides = {
        k: v
        for k, v in {
            "embedding_device": args.device,
            "embedding_batch_size": args.batch_size,
        }.items()
        if v is not None
    }
    encoder = load_encoder(s.model_copy(update=overrides))
    with connect() as conn:
        stats = job.run(
            conn,
            encoder,
            table=args.table,
            fetch_size=args.fetch_size or s.embedding_fetch_size,
            limit=args.limit,
        )
    secs = stats.seconds
    print(
        f"embedded={stats.embedded:,} skipped_changed={stats.skipped_changed:,} "
        f"batches={stats.batches} rate={stats.rows_per_second:.0f}/s "
        f"in {stats.wall_seconds:.0f}s (fetch {secs['fetch']:.0f}s, encode {secs['encode']:.0f}s, "
        f"write {secs['write']:.0f}s overlapped, waited on writes {secs['wait']:.0f}s)"
    )


def cmd_status(args: argparse.Namespace) -> None:
    ident = job.table_ident(args.table)
    with connect() as conn:
        job.ensure_schema(conn)
        conn.commit()
        from psycopg import sql

        rows = conn.execute(
            sql.SQL(
                "SELECT coalesce(embedding_model, '(pending)'), count(*), max(embedded_at) "
                "FROM {} GROUP BY 1 ORDER BY 1"
            ).format(ident)
        ).fetchall()
        print(f"{args.table}:")
        for model, n, last in rows:
            print(f"  {model:32s} {n:>12,}  last={last}")
        print("checkpoints:")
        for r in conn.execute(
            "SELECT model, status, last_complaint_id, last_chunk_no, rows_embedded, updated_at "
            "FROM ops.embedding_checkpoints WHERE target_table = %s ORDER BY model",
            (args.table,),
        ):
            print(f"  {r[0]:32s} {r[1]:9s} after=({r[2]}, {r[3]}) sweep={r[4]:,} at {r[5]}")


def cmd_reset(args: argparse.Namespace) -> None:
    if not args.yes:
        sys.exit("refusing to delete vectors without --yes")
    keep = None if args.all else get_settings().embedding_model
    with connect() as conn:
        n = job.reset(conn, args.table, keep)
    print(f"cleared {n:,} vectors" + (f" not made by {keep}" if keep else ""))


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=get_settings().log_level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # The model download logs every HTTP request at INFO.
    for noisy in ("httpx", "httpx2", "huggingface_hub", "urllib3", "filelock"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    {"run": cmd_run, "status": cmd_status, "reset": cmd_reset}[args.command](args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
