"""Build the small fixture zips CI uses to exercise ingestion and dbt end to end.

The sample is real CFPB data (public domain, CC0), drawn deterministically from a loaded
warehouse so reruns produce the same files:

* tests/fixtures/ci_bulk.zip        complaints in the bulk-CSV layout, stratified by year,
                                    plus every complaint that has a sampled narrative
* tests/fixtures/ci_narratives.zip  narratives in the FOIA archive layout

Usage:  python scripts/make_ci_fixtures.py [--per-year 120] [--narratives 300]
"""

from __future__ import annotations

import argparse
import csv
import io
import zipfile
from pathlib import Path

from complaint_warehouse.db import connect

BULK_HEADER = [
    "Date received", "Product", "Sub-product", "Issue", "Sub-issue", "Company public response",
    "Company", "State", "ZIP code", "Tags", "Submitted via", "Date sent to company",
    "Company response to consumer", "Timely response?", "Complaint ID",
]  # fmt: skip
ARCHIVE_HEADER = [*BULK_HEADER[:5], "Consumer complaint narrative", *BULK_HEADER[5:]]

RAW_COLUMNS = {
    "Date received": "date_received",
    "Product": "product",
    "Sub-product": "sub_product",
    "Issue": "issue",
    "Sub-issue": "sub_issue",
    "Company public response": "company_public_response",
    "Company": "company",
    "State": "state",
    "ZIP code": "zip_code",
    "Tags": "tags",
    "Submitted via": "submitted_via",
    "Date sent to company": "date_sent_to_company",
    "Company response to consumer": "company_response",
    "Timely response?": "timely",
    "Complaint ID": "complaint_id",
    "Consumer complaint narrative": "narrative",
}

SAMPLE_SQL = """
with narrative_ids as (
    select n.complaint_id
    from raw.complaint_narratives as n
    join raw.complaints as c using (complaint_id)
    order by md5(n.complaint_id::text)
    limit %(narratives)s
),
by_year as (
    select complaint_id
    from (
        select
            complaint_id,
            row_number() over (
                partition by extract(year from date_received)
                order by md5(complaint_id::text)
            ) as rn
        from raw.complaints
    ) ranked
    where rn <= %(per_year)s
),
picked as (
    select complaint_id from narrative_ids
    union
    select complaint_id from by_year
)
select c.*, n.narrative
from raw.complaints as c
join picked using (complaint_id)
left join raw.complaint_narratives as n using (complaint_id)
where %(with_narrative_only)s = false or n.narrative is not null
order by c.complaint_id
"""


def _write_zip(path: Path, member: str, header: list[str], rows: list[dict]) -> None:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(header)
    for row in rows:
        writer.writerow(
            "" if row[RAW_COLUMNS[h]] is None else str(row[RAW_COLUMNS[h]]) for h in header
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(member, buf.getvalue())
    print(f"wrote {path} ({len(rows)} rows, {path.stat().st_size / 1024:.0f} KiB)")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--per-year", type=int, default=120)
    parser.add_argument("--narratives", type=int, default=300)
    parser.add_argument("--out", type=Path, default=Path("tests/fixtures"))
    args = parser.parse_args()

    params = {"per_year": args.per_year, "narratives": args.narratives}
    with connect() as conn, conn.cursor() as cur:
        cur.execute(SAMPLE_SQL, params | {"with_narrative_only": False})
        cols = [d.name for d in cur.description]
        rows = [dict(zip(cols, r, strict=True)) for r in cur.fetchall()]

    _write_zip(args.out / "ci_bulk.zip", "complaints.csv", BULK_HEADER, rows)
    narrative_rows = [r for r in rows if r["narrative"]]
    _write_zip(args.out / "ci_narratives.zip", "CCDB_Export_CI.csv", ARCHIVE_HEADER, narrative_rows)


if __name__ == "__main__":
    main()
