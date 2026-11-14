import csv
import io
import os
import zipfile
from pathlib import Path

import pytest

CSV_HEADER = [
    "Date received", "Product", "Sub-product", "Issue", "Sub-issue",
    "Consumer complaint narrative", "Company public response", "Company", "State", "ZIP code",
    "Tags", "Submitted via", "Date sent to company", "Company response to consumer",
    "Timely response?", "Complaint ID",
]  # fmt: skip


def csv_row(complaint_id: int, received: str = "2026-08-04", narrative: str = "", **over) -> dict:
    row = {
        "Date received": received,
        "Product": "Credit card",
        "Sub-product": "General-purpose credit card or charge card",
        "Issue": "Problem with a purchase shown on your statement",
        "Sub-issue": "",
        "Consumer complaint narrative": narrative,
        "Company public response": "",
        "Company": "EXAMPLE BANK",
        "State": "TX",
        "ZIP code": "77573",
        "Tags": "",
        "Submitted via": "Web",
        "Date sent to company": received,
        "Company response to consumer": "Closed with explanation",
        "Timely response?": "Yes",
        "Complaint ID": str(complaint_id),
    }
    row.update(over)
    return row


def write_zip(path: Path, rows: list[dict], member: str = "export.csv") -> Path:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=CSV_HEADER)
    writer.writeheader()
    writer.writerows(rows)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(member, buf.getvalue())
    return path


@pytest.fixture
def db_conn():
    """A connection to the real database. Tests must not commit unless they clean up."""
    if not os.getenv("DATABASE_URL"):
        pytest.skip("DATABASE_URL not set")
    from complaint_warehouse.db import connect
    from complaint_warehouse.ingest.loader import ensure_schema

    conn = connect()
    ensure_schema(conn)
    conn.commit()
    yield conn
    conn.rollback()
    conn.close()
