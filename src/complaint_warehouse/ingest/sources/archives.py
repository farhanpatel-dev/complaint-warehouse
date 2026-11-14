"""Catalogue of the CFPB Consumer Complaint Database Narratives Archive.

Source: https://www.consumerfinance.gov/foia-requests/foia-electronic-reading-room/
        cfpb-consumer-complaint-database-narratives-archive/

The CFPB stopped publishing narratives on 2026-08-14 and moved everything it had published into
these 21 files. The archive is frozen, so I list the files explicitly with their coverage
instead of scraping the page: a load is reproducible and `--start/--end` can pick only the
files that overlap the requested dates.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True)
class Archive:
    number: int
    stem: str
    start: date
    end: date

    @property
    def file_name(self) -> str:
        return f"{self.stem}.zip"

    def url(self, base_url: str) -> str:
        return base_url.rstrip("/") + "/" + self.file_name

    def overlaps(self, start: date | None, end: date | None) -> bool:
        return (start is None or self.end >= start) and (end is None or self.start <= end)


def _a(n: int, stem: str, start: str, end: str) -> Archive:
    return Archive(n, f"CCDB_Export_{n}_{stem}", date.fromisoformat(start), date.fromisoformat(end))


NARRATIVE_ARCHIVES: tuple[Archive, ...] = (
    _a(1, "December_2011_through_April_2018", "2011-12-01", "2018-04-30"),
    _a(2, "May_2018_through_April_2021", "2018-05-01", "2021-04-30"),
    _a(3, "May_2021_through_October_2022", "2021-05-01", "2022-10-31"),
    _a(4, "November_2022_through_August_2023", "2022-11-01", "2023-08-31"),
    _a(5, "September_2023_through_March_2024", "2023-09-01", "2024-03-31"),
    _a(6, "April_2024_through_July_2024", "2024-04-01", "2024-07-31"),
    _a(7, "August_2024_through_October_2024", "2024-08-01", "2024-10-31"),
    _a(8, "November_2024_through_December_2024", "2024-11-01", "2024-12-31"),
    _a(9, "January_2025_through_February_2025", "2025-01-01", "2025-02-28"),
    _a(10, "March_2025_through_April_2025", "2025-03-01", "2025-04-30"),
    _a(11, "May_2025_through_June_2025", "2025-05-01", "2025-06-30"),
    _a(12, "July_2025_through_August_2025", "2025-07-01", "2025-08-31"),
    _a(13, "September_2025_through_October_2025", "2025-09-01", "2025-10-31"),
    _a(14, "November_2025_through_December_2025", "2025-11-01", "2025-12-31"),
    _a(15, "January_2026_through_February_2026", "2026-01-01", "2026-02-28"),
    _a(16, "March_2026", "2026-03-01", "2026-03-31"),
    _a(17, "April_2026", "2026-04-01", "2026-04-30"),
    _a(18, "May_2026", "2026-05-01", "2026-05-31"),
    _a(19, "June_2026", "2026-06-01", "2026-06-30"),
    _a(20, "July_2026", "2026-07-01", "2026-07-31"),
    _a(21, "August_2026", "2026-08-01", "2026-08-14"),
)


def select_archives(
    start: date | None = None,
    end: date | None = None,
    numbers: set[int] | None = None,
) -> list[Archive]:
    return [
        a
        for a in NARRATIVE_ARCHIVES
        if a.overlaps(start, end) and (numbers is None or a.number in numbers)
    ]
