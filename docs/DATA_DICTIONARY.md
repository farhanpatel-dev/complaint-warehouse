# Data dictionary

Every table and column in the warehouse, by schema, in data-flow order. Row counts are from
my full load on 2026-11-17: CFPB data received from 2011-12-01 to 2026-11-14, and
narratives up to 2026-08-14. Column docs are also in the dbt docs site (`make dbt-docs`);
this page adds the caveats that matter when reading the numbers.

Source: the [CFPB Consumer Complaint Database](https://www.consumerfinance.gov/data-research/consumer-complaints/)
(public domain, CC0). It records complaints as consumers filed them. It is not a sample,
and the CFPB does not verify every claim. A complaint count measures complaints, not
misconduct.

## Conventions

| Convention | Meaning |
|---|---|
| `*_key` | 64-bit hash surrogate key (`hash_key` macro). Stable across rebuilds. |
| `-1` | Unknown member. Every dimension has one; a fact with `-1` means a lookup failed. Currently 0 facts. |
| `[valid_from, valid_to)` | Half-open SCD2 validity. The current version has `valid_to = 9999-12-31`, and first versions are backdated to `1900-01-01` (ADR 0003). |
| Sentinels | Dimension natural keys are never NULL: `'Unknown'` company or product, `'(none)'` sub-product, `'--'` state, `'-----'` ZIP. Plain hash joins then work. |
| `last_changed_at` / `source_changed_at` | When the business content last changed. Drives every incremental model. |

## Data-quality notes (read these first)

1. **"In progress" complaints are published as timely.** The CFPB sets
   `Timely response? = Yes` on complaints the company has not finished answering. All
   651,175 in-progress complaints have `timely_flag = true`. They are concentrated in recent
   months: 14% of September 2026, 49% of October and 74% of November. The fact keeps the
   flag as published. Every mart computes timely rates over **responded** complaints only
   (`response_type <> 'In progress'`); use `responded` as the denominator.
2. **No disputes.** `disputed_flag` is always NULL. The CFPB stopped collecting disputes in
   2017, and no current source has the column (ADR 0005).
3. **Narratives stop at 2026-08-14.** The CFPB stopped publishing narratives, so complaints
   received after that date have none and are not text-searchable (ADR 0005).
4. **Not every narrative becomes search chunks.** 3,851,415 narratives were loaded:
   - **777** are only redaction tokens (`XXXX`, `XX/XX/XXXX`). Their cleaned text is
     empty, so they have no chunks.
   - **74** have no matching complaint in `raw.complaints`: the archive has narratives for
     complaints the live sources no longer publish. Their chunks are embedded like any
     other, but they have no row in `complaint_facets` or `fact_complaint`, so search (which joins facets)
     never returns them.

   Result: 3,851,341 complaints have `has_narrative = true`, and 3,850,638 distinct
   complaints have chunks.
5. **ZIPs can be masked.** For privacy, the CFPB publishes only the first three digits
   for some complaints (`'391XX'`). `zip3` and `is_zip_masked` handle both forms.
6. **Product names change by era.** Use `product_family`, not `product`, for trends
   (ADR 0003).
7. **The latest month is partial**, and its complaints are mostly still in progress.

## raw: landed by the ingestion job

### `raw.complaints`: 19.17M rows, one per complaint

The latest version of each complaint from the search API or the bulk CSV, upserted by
`complaint_id`. Values are kept as the source delivers them (text), except the key and
the dates.

| Column | Type | Notes |
|---|---|---|
| `complaint_id` | bigint PK | CFPB id |
| `date_received` | date | API timestamps are UTC; only the date part is kept |
| `date_sent_to_company` | date | |
| `product`, `sub_product`, `issue`, `sub_issue` | text | CFPB taxonomy of the complaint's era |
| `company` | text | Company name as published |
| `state`, `zip_code` | text | Raw; cleaned in staging |
| `tags` | text | `Older American`, `Servicemember`, or both |
| `submitted_via` | text | Web, Phone, Referral, ... |
| `company_response`, `company_public_response` | text | |
| `timely` | text | `Yes` / `No` (see note 1) |
| `consumer_disputed` | text | Always empty from current sources |
| `source` | text | `api` or `bulk` |
| `record_hash` | text | SHA-256 of the normalised business fields; unchanged rows are skipped |
| `load_id` | bigint | The `ops.load_runs` run that last wrote the row |
| `first_seen_at`, `last_changed_at` | timestamptz | `last_changed_at` moves only when `record_hash` changes |

### `raw.complaint_narratives`: 3.85M rows

| Column | Type | Notes |
|---|---|---|
| `complaint_id` | bigint PK | |
| `date_received` | date | |
| `narrative` | text | Consumer-written text with CFPB redactions |
| `source_file` | text | Archive file it came from |
| `record_hash`, `load_id`, `first_seen_at`, `last_changed_at` | | As above |

## ops: pipeline bookkeeping

| Table | Grain | Purpose |
|---|---|---|
| `ops.load_runs` | one row per job run | source, params (JSON), status (`running` / `succeeded` / `failed`), rows read / inserted / updated, error |
| `ops.watermarks` | one row per key | `high_watermark`: the last fully loaded `date_received` window. It only moves forward. |
| `ops.ingested_files` | one row per file | file name, SHA-256, size, rows. A file is skipped when its hash matches. |
| `ops.embedding_checkpoints` | (target table, model) | last `(complaint_id, chunk_no)` embedded, rows in this sweep, `running` / `complete` |

## reference: dbt seeds

| Seed | Rows | Content |
|---|---|---|
| `us_states` | 63 | Postal code → name, Census region and division. Territories, freely associated states and military codes get their own regions. |
| `product_taxonomy` | 21 | Every CFPB product name, current and retired → one of 12 `product_family` values, plus `is_current_name` |
| `company_groups` | 26 | My curated parent group and industry for the highest-volume companies. All others are `Unclassified`. |

## snapshots: SCD2 history (state; back it up)

`snap_company` (8,214 rows) and `snap_product` (127 rows): dbt `check` snapshots of the
curated attributes, with dbt's `dbt_valid_from` / `dbt_valid_to` columns. These cannot be
rebuilt from source.

## staging: views

| Model | Notes |
|---|---|
| `stg_cfpb__complaints` | Typed and cleaned complaints. Sentinels for missing natural keys. `Yes` / `No` become booleans, and anything else becomes NULL. Adds `zip3`, `is_zip_masked`, `is_older_american`, `is_servicemember`, `days_to_company`. |
| `stg_cfpb__narratives` | `narrative` with whitespace normalised. `narrative_clean` has the redaction runs (`\mX{2,}(/X{2,})*\M`) removed and feeds chunking, full text and embeddings. `narrative_chars`. |
| `int_company_attributes` | Distinct company names left-joined to `company_groups`. Defaults: parent = name, industry = `Unclassified`. |
| `int_product_attributes` | Distinct `(product, sub_product)` joined to `product_taxonomy`. `product_nk = 'product \| sub_product'`. |

## warehouse: star schema

### `fact_complaint`: 19,170,856 rows, one per complaint

Range-partitioned by month on `date_received`, with 180 monthly partitions from 2011-12 to
2026-11. Dimension keys point to the version valid on `date_received` ("as was").

| Column | Type | Notes |
|---|---|---|
| `complaint_id` | bigint | Degenerate dimension. Unique with `date_received` (partition key). |
| `date_key` | int | FK → `dim_date` (YYYYMMDD) |
| `date_received` | date | Partition key |
| `company_key` | bigint | FK → `dim_company` |
| `product_key` | bigint | FK → `dim_product` |
| `geo_key` | bigint | FK → `dim_geo` |
| `response_type` | text | Closed with explanation / with monetary relief / with non-monetary relief, In progress, Untimely response, and older values (Closed, Closed with relief, Closed without relief) |
| `timely_flag` | boolean | As published; true for in-progress complaints (note 1) |
| `disputed_flag` | boolean | Always NULL (note 2) |
| `submitted_via` | text | Channel |
| `days_to_company` | int | Days from receipt to forwarding |
| `is_older_american`, `is_servicemember` | boolean | From tags |
| `has_narrative` | boolean | A narrative exists in the archive |
| `source_changed_at` | timestamptz | Latest change of the complaint or its narrative; the incremental watermark |

Indexes: unique `(complaint_id, date_received)`, `(company_key, date_received)`,
`(product_key, date_received)`, `(geo_key)`, `(source_changed_at)`.

### Dimensions

| Table | Rows | Grain | Columns |
|---|---|---|---|
| `dim_date` | 6,971 | day, 2011-12-01 to 2030-12-31 | `date_key` (YYYYMMDD), `date_day`, `year`, `quarter`, `month`, `month_name`, `month_start`, `quarter_start`, `iso_year`, `iso_week`, `week_start`, `iso_day_of_week`, `day_name`, `is_weekend`, `day_of_month`, `day_of_year`, `is_month_end` |
| `dim_company` | 8,215 | company version (SCD2) | `company_key`, `company_name`, `parent_group`, `industry`, `valid_from`, `valid_to`, `is_current` |
| `dim_product` | 128 | (product, sub_product) version (SCD2) | `product_key`, `product_nk`, `product`, `sub_product`, `product_family`, `is_current_name`, `valid_from`, `valid_to`, `is_current` |
| `dim_geo` | 42,729 | (state_code, zip_code) | `geo_key`, `state_code`, `zip_code`, `zip3`, `is_zip_masked`, `state_name`, `census_region`, `census_division`, `jurisdiction_type` |

Row counts include the unknown member. In the current data every company and product has
exactly one version (ADR 0003).

## search: hybrid search

### `search.complaint_text`: 5,595,687 rows, one per (complaint_id, chunk_no)

Narratives split into 200-word windows that overlap by 40 words (`chunk_words`,
`chunk_overlap_words` in `dbt_project.yml`). Incremental with delete+insert by complaint,
and `full_refresh = false`, so a careless `--full-refresh` cannot drop four hours of
embeddings. 32 GB with indexes, most of it TOASTed vectors.

| Column | Type | Notes |
|---|---|---|
| `complaint_id` | bigint | |
| `chunk_no` | int | 0-based position |
| `narrative` | text | Chunk text, redactions removed |
| `tsv` | tsvector | `to_tsvector('english', narrative)`, GIN-indexed |
| `embedding` | vector(768) | `BAAI/bge-base-en-v1.5`, L2-normalised. NULL until embedded (0 pending now). |
| `embedding_model` | text | Model that produced the vector. The job refuses to mix models. |
| `embedded_at` | timestamptz | |
| `source_changed_at` | timestamptz | Narrative's `last_changed_at`; the incremental watermark |

Indexes: unique `(complaint_id, chunk_no)`, GIN `(tsv)`, and a partial
`(complaint_id, chunk_no) WHERE embedding IS NULL` for the embedding job. There is no
vector index on the full corpus yet (ADR 0004).

### `search.complaint_facets`: 3,851,341 rows, one per complaint with a narrative

Unpartitioned lookup that search filters and displays from, so a filter probes one index
instead of 180 partitions (ADR 0002). `product_family` comes from the **current** taxonomy.

`complaint_id`, `date_received`, `company_name`, `product`, `sub_product`,
`product_family`, `issue`, `state_code`, `response_type`, `timely_flag` (as published;
see note 1).

### `search.hybrid_search(...)`: function

Returns ranked complaints. Its parameters (query text and embedding, weights, `rrf_k`,
`candidate_count`, `keyword_match`, filters) and outputs are described in
[LLD](LLD.md#hybrid-search-function).

## marts: materialized views, "as is"

Each mart groups history by the **current** company and product attributes. Timely rates
exclude in-progress complaints (note 1).

| View | Rows | Grain | Columns |
|---|---|---|---|
| `mv_company_monthly` | ~214k | month × company | `month`, `company_name`, `parent_group`, `industry`, `complaints`, `responded`, `timely_responses`, `timely_rate`, `monetary_relief`, `non_monetary_relief`, `in_progress`, `with_narrative` |
| `mv_product_monthly` | ~1.7k | month × family × product | `month`, `product_family`, `product`, `complaints`, `responded`, `timely_rate`, `monetary_relief`, `with_narrative` |
| `mv_state_monthly` | ~10.6k | month × state | `month`, `state_code`, `state_name`, `census_region`, `complaints`, `responded`, `timely_rate`, `older_american`, `servicemember` |
| `mv_daily_volume` | ~1.59M | day × company × family | `date_received`, `company_name`, `product_family`, `complaints`, `responded`, `timely_responses` |

`timely_rate = timely_responses / responded`. Days with no complaints are absent from
`mv_daily_volume`; the analytics queries densify against `dim_date` where gaps matter.
