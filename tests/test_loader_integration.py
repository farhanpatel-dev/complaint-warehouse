"""Idempotency and change detection against a real Postgres (run with DATABASE_URL set)."""

from datetime import date

import pytest

from complaint_warehouse.ingest.loader import (
    advance_watermark,
    get_watermark,
    merge_complaints,
    merge_narratives,
)
from complaint_warehouse.ingest.pipeline import load_narrative_file
from complaint_warehouse.ingest.records import from_csv_row
from tests.conftest import csv_row, write_zip

pytestmark = pytest.mark.integration

# Far above real CFPB ids so tests never touch real rows.
BASE_ID = 9_900_000_000


def _complaints(*rows):
    return [from_csv_row(r)[0] for r in rows]


def test_merge_is_idempotent_and_detects_changes(db_conn):
    first = _complaints(csv_row(BASE_ID + 1), csv_row(BASE_ID + 2))
    s1 = merge_complaints(db_conn, first, "test", load_id=0)
    assert (s1.read, s1.inserted, s1.updated, s1.unchanged) == (2, 2, 0, 0)

    s2 = merge_complaints(db_conn, first, "test", load_id=0)
    assert (s2.inserted, s2.updated, s2.unchanged) == (0, 0, 2)

    changed = _complaints(
        csv_row(BASE_ID + 1, **{"Company response to consumer": "Closed with monetary relief"}),
        csv_row(BASE_ID + 2),
        csv_row(BASE_ID + 3),
    )
    s3 = merge_complaints(db_conn, changed, "test", load_id=0)
    assert (s3.inserted, s3.updated, s3.unchanged) == (1, 1, 1)

    response = db_conn.execute(
        "SELECT company_response FROM raw.complaints WHERE complaint_id = %s", (BASE_ID + 1,)
    ).fetchone()[0]
    assert response == "Closed with monetary relief"


def test_duplicate_ids_within_a_batch_do_not_fail(db_conn):
    dupes = _complaints(csv_row(BASE_ID + 10), csv_row(BASE_ID + 10))
    stats = merge_complaints(db_conn, dupes, "test", load_id=0)
    assert (stats.read, stats.inserted) == (1, 1)


def test_narrative_merge(db_conn):
    pairs = [from_csv_row(csv_row(BASE_ID + 20, narrative="first version"))]
    s1 = merge_narratives(db_conn, [n for _, n in pairs], "test.zip", load_id=0)
    s2 = merge_narratives(db_conn, [n for _, n in pairs], "test.zip", load_id=0)
    assert (s1.inserted, s2.unchanged) == (1, 1)


def test_watermark_only_moves_forward(db_conn):
    key = "test-watermark"
    advance_watermark(db_conn, key, date(2026, 5, 1))
    advance_watermark(db_conn, key, date(2026, 1, 1))
    assert get_watermark(db_conn, key) == date(2026, 5, 1)
    advance_watermark(db_conn, key, date(2026, 6, 1))
    assert get_watermark(db_conn, key) == date(2026, 6, 1)


def test_narrative_file_load_skips_rows_without_narrative_and_reruns_are_skipped(db_conn, tmp_path):
    ids = [BASE_ID + 30, BASE_ID + 31, BASE_ID + 32]
    path = write_zip(
        tmp_path / f"TEST_ARCHIVE_{BASE_ID}.zip",
        [
            csv_row(ids[0], narrative="I was charged a fee twice."),
            csv_row(ids[1], narrative=""),
            csv_row(ids[2], narrative="My dispute was ignored."),
        ],
    )
    try:
        run = load_narrative_file(db_conn, path, start=None, end=None, batch_size=2)
        assert run is not None
        assert (run.stats.read, run.stats.inserted) == (2, 2)
        status = db_conn.execute(
            "SELECT status, rows_inserted FROM ops.load_runs WHERE load_id = %s", (run.load_id,)
        ).fetchone()
        assert status == ("succeeded", 2)

        assert load_narrative_file(db_conn, path, start=None, end=None, batch_size=2) is None
    finally:
        db_conn.rollback()
        db_conn.execute("DELETE FROM raw.complaint_narratives WHERE complaint_id = ANY(%s)", (ids,))
        db_conn.execute("DELETE FROM ops.ingested_files WHERE file_name = %s", (path.name,))
        db_conn.execute(
            "DELETE FROM ops.load_runs WHERE source = 'narratives' AND params->>'file' = %s",
            (path.name,),
        )
        db_conn.commit()
