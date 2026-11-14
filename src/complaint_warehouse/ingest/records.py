"""Normalisation of API hits and CSV rows into one canonical record shape."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import astuple, dataclass
from datetime import date, datetime
from typing import Any

# Column order here is the column order of raw.complaints (business fields only).
COMPLAINT_FIELDS: tuple[str, ...] = (
    "complaint_id",
    "date_received",
    "date_sent_to_company",
    "product",
    "sub_product",
    "issue",
    "sub_issue",
    "company",
    "state",
    "zip_code",
    "tags",
    "submitted_via",
    "company_response",
    "company_public_response",
    "timely",
    "consumer_disputed",
)

# Bulk CSV and Narratives Archive headers -> canonical names.
CSV_COLUMNS: dict[str, str] = {
    "Complaint ID": "complaint_id",
    "Date received": "date_received",
    "Date sent to company": "date_sent_to_company",
    "Product": "product",
    "Sub-product": "sub_product",
    "Issue": "issue",
    "Sub-issue": "sub_issue",
    "Company": "company",
    "State": "state",
    "ZIP code": "zip_code",
    "Tags": "tags",
    "Submitted via": "submitted_via",
    "Company response to consumer": "company_response",
    "Company public response": "company_public_response",
    "Timely response?": "timely",
    "Consumer disputed?": "consumer_disputed",
    "Consumer complaint narrative": "narrative",
}

# Search API `_source` keys -> canonical names (most already match).
API_COLUMNS: dict[str, str] = {name: name for name in COMPLAINT_FIELDS} | {
    "complaint_what_happened": "narrative",
}


class RecordError(ValueError):
    """A source row that cannot be normalised (missing id or date)."""


def clean_text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, list | tuple):
        value = ", ".join(str(v).strip() for v in value if v not in (None, ""))
    text = str(value).strip()
    return text or None


def parse_date(value: Any) -> date | None:
    """Accept ISO dates, ISO timestamps (date part, which is UTC for the API) and MM/DD/YYYY."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    if not text:
        return None
    if len(text) >= 10 and text[4] == "-":
        return date.fromisoformat(text[:10])
    return datetime.strptime(text, "%m/%d/%Y").date()


def _hash(values: tuple[Any, ...]) -> str:
    payload = json.dumps(values, default=str, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class ComplaintRecord:
    complaint_id: int
    date_received: date
    date_sent_to_company: date | None
    product: str | None
    sub_product: str | None
    issue: str | None
    sub_issue: str | None
    company: str | None
    state: str | None
    zip_code: str | None
    tags: str | None
    submitted_via: str | None
    company_response: str | None
    company_public_response: str | None
    timely: str | None
    consumer_disputed: str | None

    @property
    def record_hash(self) -> str:
        # complaint_id is the key, not content; leaving it out keeps the hash about the data.
        return _hash(astuple(self)[1:])

    def as_row(self) -> tuple[Any, ...]:
        return astuple(self)


@dataclass(frozen=True, slots=True)
class NarrativeRecord:
    complaint_id: int
    date_received: date
    narrative: str

    @property
    def record_hash(self) -> str:
        return _hash((self.date_received, self.narrative))


def _canonical(raw: Mapping[str, Any], column_map: Mapping[str, str]) -> dict[str, Any]:
    return {column_map[k]: v for k, v in raw.items() if k in column_map}


def _build(canon: dict[str, Any]) -> tuple[ComplaintRecord, NarrativeRecord | None]:
    raw_id = clean_text(canon.get("complaint_id"))
    if raw_id is None:
        raise RecordError("missing complaint_id")
    try:
        complaint_id = int(raw_id)
    except ValueError as exc:
        raise RecordError(f"non-numeric complaint_id {raw_id!r}") from exc

    received = parse_date(canon.get("date_received"))
    if received is None:
        raise RecordError(f"complaint {complaint_id} has no date_received")

    complaint = ComplaintRecord(
        complaint_id=complaint_id,
        date_received=received,
        date_sent_to_company=parse_date(canon.get("date_sent_to_company")),
        **{
            name: clean_text(canon.get(name))
            for name in COMPLAINT_FIELDS
            if name not in {"complaint_id", "date_received", "date_sent_to_company"}
        },
    )
    narrative_text = clean_text(canon.get("narrative"))
    narrative = NarrativeRecord(complaint_id, received, narrative_text) if narrative_text else None
    return complaint, narrative


def from_csv_row(row: Mapping[str, Any]) -> tuple[ComplaintRecord, NarrativeRecord | None]:
    return _build(_canonical(row, CSV_COLUMNS))


def from_api_hit(hit: Mapping[str, Any]) -> tuple[ComplaintRecord, NarrativeRecord | None]:
    return _build(_canonical(hit.get("_source", hit), API_COLUMNS))
