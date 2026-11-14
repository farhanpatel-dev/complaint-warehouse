import itertools
from datetime import date

import pytest

from complaint_warehouse.ingest.loader import batched
from complaint_warehouse.ingest.pipeline import date_windows, plan_incremental_range
from complaint_warehouse.ingest.sources.archives import NARRATIVE_ARCHIVES, select_archives

D = date


def test_date_windows_cover_range_without_gaps_or_overlap():
    windows = date_windows(D(2026, 1, 30), D(2026, 2, 3), 2)
    assert windows == [
        (D(2026, 1, 30), D(2026, 1, 31)),
        (D(2026, 2, 1), D(2026, 2, 2)),
        (D(2026, 2, 3), D(2026, 2, 3)),
    ]


def test_date_windows_single_day():
    assert date_windows(D(2026, 1, 1), D(2026, 1, 1), 7) == [(D(2026, 1, 1), D(2026, 1, 1))]


def test_date_windows_rejects_zero():
    with pytest.raises(ValueError):
        date_windows(D(2026, 1, 1), D(2026, 1, 2), 0)


def test_first_run_starts_at_default():
    assert plan_incremental_range(
        watermark=None, lookback_days=30, default_start=D(2025, 1, 1), today=D(2026, 11, 14)
    ) == (D(2025, 1, 1), D(2026, 11, 14))


def test_later_runs_resume_from_watermark_minus_lookback():
    assert plan_incremental_range(
        watermark=D(2026, 11, 10),
        lookback_days=30,
        default_start=D(2025, 1, 1),
        today=D(2026, 11, 14),
    ) == (D(2026, 10, 11), D(2026, 11, 14))


def test_explicit_range_wins_over_watermark():
    assert plan_incremental_range(
        watermark=D(2026, 11, 10),
        lookback_days=30,
        default_start=D(2025, 1, 1),
        today=D(2026, 11, 14),
        start=D(2024, 1, 1),
        end=D(2024, 1, 31),
    ) == (D(2024, 1, 1), D(2024, 1, 31))


def test_inverted_range_is_rejected():
    with pytest.raises(ValueError):
        plan_incremental_range(
            watermark=None, lookback_days=0, default_start=D(2027, 1, 1), today=D(2026, 1, 1)
        )


def test_archive_catalogue_is_contiguous():
    for prev, nxt in itertools.pairwise(NARRATIVE_ARCHIVES):
        assert (nxt.start - prev.end).days == 1, (prev.stem, nxt.stem)
    assert len(NARRATIVE_ARCHIVES) == 21


def test_select_archives_by_date_overlap():
    assert [a.number for a in select_archives(D(2026, 7, 15), None)] == [20, 21]
    assert [a.number for a in select_archives(D(2024, 3, 31), D(2024, 4, 1))] == [5, 6]
    assert [a.number for a in select_archives(numbers={1, 21})] == [1, 21]


def test_batched():
    assert list(batched(range(5), 2)) == [[0, 1], [2, 3], [4]]
    assert list(batched([], 3)) == []
