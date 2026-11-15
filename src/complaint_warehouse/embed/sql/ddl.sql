-- Checkpoint for the embedding job. Idempotent; applied on every run.
--
-- One row per (target table, model). The job walks the table in (complaint_id, chunk_no)
-- order and commits the last key it embedded in the same transaction as the vectors, so
-- after a crash it resumes exactly where the last commit left off. When a sweep reaches the
-- end the key resets to the start and status becomes 'complete'; the next run then picks up
-- chunks dbt inserted or re-chunked since (they arrive with a NULL embedding).

CREATE TABLE IF NOT EXISTS ops.embedding_checkpoints (
    target_table TEXT NOT NULL,
    model TEXT NOT NULL,
    last_complaint_id BIGINT NOT NULL DEFAULT -1,
    last_chunk_no INTEGER NOT NULL DEFAULT -1,
    rows_embedded BIGINT NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'running'
    CHECK (status IN ('running', 'complete')),
    load_id BIGINT REFERENCES ops.load_runs (load_id),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (target_table, model)
);

COMMENT ON TABLE ops.embedding_checkpoints IS
'Resume point of the embedding job per target table and model.';
COMMENT ON COLUMN ops.embedding_checkpoints.rows_embedded IS
'Chunks embedded in the current sweep; reset when a new sweep starts.';
