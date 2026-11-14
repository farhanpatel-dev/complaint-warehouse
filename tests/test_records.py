from datetime import date

import pytest

from complaint_warehouse.ingest.records import (
    RecordError,
    clean_text,
    from_api_hit,
    from_csv_row,
    parse_date,
)
from tests.conftest import csv_row

API_HIT = {
    "_id": "8614742",
    "_source": {
        "product": "Checking or savings account",
        "date_sent_to_company": "2024-03-25T19:56:46.000Z",
        "issue": "Managing an account",
        "sub_product": "Checking account",
        "zip_code": "29303",
        "tags": None,
        "complaint_id": "8614742",
        "timely": "Yes",
        "company_response": "Closed with explanation",
        "submitted_via": "Referral",
        "company": "FIRST HORIZON BANK",
        "date_received": "2024-03-01T20:15:18.000Z",
        "state": "SC",
        "company_public_response": None,
        "sub_issue": "Problem making or receiving payments",
        "date_indexed": "2026-10-18T09:07:23+00:00",
    },
    "sort": [1709324118000, "8614742"],
}


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("2024-03-01", date(2024, 3, 1)),
        ("2024-03-01T20:15:18.000Z", date(2024, 3, 1)),
        ("03/01/2024", date(2024, 3, 1)),
        ("", None),
        (None, None),
    ],
)
def test_parse_date(raw, expected):
    assert parse_date(raw) == expected


def test_clean_text_handles_blank_and_lists():
    assert clean_text("  ") is None
    assert clean_text(" Web ") == "Web"
    assert clean_text(["Older American", "Servicemember"]) == "Older American, Servicemember"


def test_api_hit_normalises_timestamps_and_ignores_unknown_fields():
    complaint, narrative = from_api_hit(API_HIT)
    assert complaint.complaint_id == 8614742
    assert complaint.date_received == date(2024, 3, 1)
    assert complaint.date_sent_to_company == date(2024, 3, 25)
    assert complaint.company == "FIRST HORIZON BANK"
    assert complaint.tags is None
    assert narrative is None


def test_csv_row_with_narrative():
    complaint, narrative = from_csv_row(csv_row(42, narrative="  They charged me twice.  "))
    assert complaint.complaint_id == 42
    assert narrative is not None
    assert narrative.narrative == "They charged me twice."
    assert narrative.date_received == complaint.date_received


def test_same_complaint_from_api_and_csv_hashes_identically():
    api_complaint, _ = from_api_hit(API_HIT)
    row = {
        "Complaint ID": "8614742",
        "Date received": "2024-03-01",
        "Date sent to company": "2024-03-25",
        "Product": "Checking or savings account",
        "Sub-product": "Checking account",
        "Issue": "Managing an account",
        "Sub-issue": "Problem making or receiving payments",
        "Company": "FIRST HORIZON BANK",
        "State": "SC",
        "ZIP code": "29303",
        "Tags": "",
        "Submitted via": "Referral",
        "Company response to consumer": "Closed with explanation",
        "Company public response": "",
        "Timely response?": "Yes",
    }
    csv_complaint, _ = from_csv_row(row)
    assert csv_complaint == api_complaint
    assert csv_complaint.record_hash == api_complaint.record_hash


def test_hash_changes_when_business_field_changes():
    a, _ = from_csv_row(csv_row(1))
    b, _ = from_csv_row(csv_row(1, **{"Company response to consumer": "In progress"}))
    assert a.record_hash != b.record_hash


@pytest.mark.parametrize(
    "override",
    [{"Complaint ID": ""}, {"Complaint ID": "abc"}, {"Date received": ""}],
)
def test_invalid_rows_raise(override):
    with pytest.raises(RecordError):
        from_csv_row(csv_row(1, **override))
