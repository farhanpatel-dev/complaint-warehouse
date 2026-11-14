# ADR 0005: Source narratives from the CFPB FOIA Narratives Archive

- **Status:** Accepted
- **Date:** 2026-11-14
- **Deciders:** Farhan Patel

## Context

Semantic search, the index study and the search-quality evaluation all need free text. In
the CFPB Consumer Complaint Database that text is the consumer-written narrative.

When I started ingestion I checked the live sources before writing the loader:

- The search API no longer returns `complaint_what_happened`. Passing it as a search field
  returns `"complaint_what_happened" is not a valid choice.`, and records from 2019 come back
  without it too, so the field is gone, not just empty.
- The bulk export `complaints.csv.zip` has 15 columns. `Consumer complaint narrative` (and
  `Consumer disputed?`) are not among them.

The CFPB announced on 2026-08-14 that it would stop publishing narratives, and it moved
everything it had published into a Narratives Archive in its FOIA electronic reading room:
21 zipped CSV exports covering complaints received from 2011-12-01 to 2026-08-14. Their
layout is the old bulk-CSV layout, narrative column included.

## Decision

I ingest from three official CFPB sources, each landing in `raw`:

| Source | Used for | Table | Load pattern |
|---|---|---|---|
| Search API | daily increments | `raw.complaints` | watermark minus 30-day lookback, per-day windows |
| `complaints.csv.zip` | full / date-bounded backfill | `raw.complaints` | batched upsert, moves the watermark |
| FOIA Narratives Archive | narratives | `raw.complaint_narratives` | once per file, skipped by SHA-256 |

Narratives live in their own table keyed by `complaint_id` instead of being a column on
`raw.complaints`. The archive only writes narratives; structured fields always come from the
live sources, which are fresher than a frozen export.

The archive catalogue is hard-coded with each file's coverage dates
(`ingest/sources/archives.py`) instead of scraped, because the archive is frozen. That keeps
loads reproducible and lets `--start/--end` pick only the files that overlap a date range.

## Consequences

- **Good:** Every input is official CFPB data under its CC0 licence. No third-party mirror
  whose provenance or completeness I would have to vouch for.
- **Good:** Splitting narratives into their own table matches reality: about 19.2M complaints
  versus a much smaller set with narratives, and two different update patterns (live versus
  frozen).
- **Trade-off:** No narratives exist for complaints received after 2026-08-14. New
  complaints are still analysed but are not searchable by text. The search corpus is
  effectively historical, which is fine for a search-quality study but would matter for a
  product.
- **Trade-off:** `disputed_flag` in `fact_complaint` will be NULL. No current source carries
  `Consumer disputed?` (the CFPB stopped collecting it in 2017 and has now dropped the column).
  I keep the column to honour the model contract and document it as unavailable.
- **Risk:** If the CFPB moves or renames the archive files, the catalogue needs updating. The
  downloader fails loudly on a 404 rather than silently loading nothing.

## Alternatives considered

1. **Kaggle / Hugging Face mirrors with narratives.** Rejected. These are older snapshots
   (about 2.4–3.1M rows) of unknown completeness, and I would be trusting someone else's
   export.
2. **Search over structured text only (product + issue + sub-issue).** Rejected as the main
   path. Those fields come from a fixed taxonomy of a few hundred values, so "semantic" search
   over them is barely different from filtering and would make the vector-versus-keyword study
   meaningless.
3. **A FOIA request for post-August narratives.** Out of scope: slow, and the result would not
   be redistributable with the project.
