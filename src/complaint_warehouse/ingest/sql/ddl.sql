-- Raw landing tables and pipeline bookkeeping. Owned by the ingestion job and
-- applied at the start of every run, so every statement must be idempotent.

-- ---------------------------------------------------------------------------
-- raw.complaints: one row per complaint, latest version seen from any source.
-- Values are kept as the source delivers them (text) except the key and the two
-- dates, which are normalised so the API (UTC timestamps) and the CSVs (dates)
-- land in one shape. Business cleaning happens in dbt staging.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS raw.complaints (
    complaint_id BIGINT PRIMARY KEY,
    date_received DATE NOT NULL,
    date_sent_to_company DATE,
    product TEXT,
    sub_product TEXT,
    issue TEXT,
    sub_issue TEXT,
    company TEXT,
    state TEXT,
    zip_code TEXT,
    tags TEXT,
    submitted_via TEXT,
    company_response TEXT,
    company_public_response TEXT,
    timely TEXT,
    consumer_disputed TEXT,
    source TEXT NOT NULL,
    record_hash TEXT NOT NULL,
    load_id BIGINT NOT NULL,
    first_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_changed_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS complaints_date_received_idx
ON raw.complaints (date_received);

-- dbt incremental models and snapshots select rows changed since their last run.
CREATE INDEX IF NOT EXISTS complaints_last_changed_at_idx
ON raw.complaints (last_changed_at);

COMMENT ON TABLE raw.complaints IS
'Latest version of each CFPB complaint (API or bulk CSV); upserted by complaint_id.';
COMMENT ON COLUMN raw.complaints.record_hash IS
'SHA-256 of the normalised business fields; used to skip no-op updates.';
COMMENT ON COLUMN raw.complaints.last_changed_at IS
'When the business fields last changed. Drives dbt incremental models and snapshots.';

-- ---------------------------------------------------------------------------
-- raw.complaint_narratives: consumer narratives from the CFPB FOIA archive.
-- CFPB stopped publishing narratives on 2026-08-14; the archive is frozen, so
-- this table is loaded once per archive file rather than incrementally.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS raw.complaint_narratives (
    complaint_id BIGINT PRIMARY KEY,
    date_received DATE NOT NULL,
    narrative TEXT NOT NULL,
    source_file TEXT NOT NULL,
    record_hash TEXT NOT NULL,
    load_id BIGINT NOT NULL,
    first_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_changed_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS complaint_narratives_date_received_idx
ON raw.complaint_narratives (date_received);

COMMENT ON TABLE raw.complaint_narratives IS
'Consumer-written complaint narratives from the CFPB Narratives Archive (FOIA reading room), Dec 2011 to 2026-08-14.';

-- ---------------------------------------------------------------------------
-- ops: run log, incremental watermarks, and loaded-file registry.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS ops.load_runs (
    load_id BIGSERIAL PRIMARY KEY,
    source TEXT NOT NULL,
    params JSONB NOT NULL DEFAULT '{}'::JSONB,
    status TEXT NOT NULL DEFAULT 'running'
    CHECK (status IN ('running', 'succeeded', 'failed')),
    started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at TIMESTAMPTZ,
    rows_read BIGINT NOT NULL DEFAULT 0,
    rows_inserted BIGINT NOT NULL DEFAULT 0,
    rows_updated BIGINT NOT NULL DEFAULT 0,
    error TEXT
);

CREATE TABLE IF NOT EXISTS ops.watermarks (
    watermark_key TEXT PRIMARY KEY,
    high_watermark DATE NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

COMMENT ON TABLE ops.watermarks IS
'Last fully loaded date_received window per key; only moves forward.';

CREATE TABLE IF NOT EXISTS ops.ingested_files (
    file_name TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    size_bytes BIGINT NOT NULL,
    rows_read BIGINT NOT NULL,
    load_id BIGINT NOT NULL REFERENCES ops.load_runs (load_id),
    loaded_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
