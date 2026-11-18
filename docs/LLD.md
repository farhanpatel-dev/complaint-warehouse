# Low-level design

How each component works, with the reasons for the details. Read the [HLD](HLD.md) first.
Table and column definitions are in the [data dictionary](DATA_DICTIONARY.md).

## 1. Repository and runtime

```
src/complaint_warehouse/
  config.py        pydantic-settings; every knob is an env var (.env.example)
  db.py            connect() for jobs; a psycopg_pool ConnectionPool for the API
  ingest/          http.py, records.py, loader.py, pipeline.py, sources/{api,files,archives}.py
  embed/           model.py (encoder), job.py (resumable batch job), sql/ddl.sql
  search/          service.py (validation, mode → weights, SQL call, response)
  api/             main.py (FastAPI: /health, /search)
  bench/           index_study, quality, analytics, knn, metrics, report
dbt/               models/{staging,intermediate,warehouse,search,marts}, snapshots, seeds, macros, tests
db/init/           extensions + schemas, run once on an empty volume
sql/analytics/     the analytics library
```

- **Docker images.** One image, two services. `app` runs uvicorn with a healthcheck.
  `worker` (profile `jobs`) runs batch commands with `docker compose run --rm worker ...`.
  The default image ships CPU torch; `docker-compose.gpu.yml` swaps in the CUDA 12.8 build.
- **Schemas** mirror the data flow: `raw → staging → warehouse → marts`, plus `search`,
  `ops`, `reference` (seeds) and `snapshots`. The ingestion job owns `raw` and `ops` and
  applies its DDL idempotently on every run. dbt owns everything else.

## 2. Ingestion

### 2.1 Record normalisation (`records.py`)

The API (JSON hits) and the CSVs (bulk and archive) use different field names and date
formats. `from_api_hit` and `from_csv_row` map both into one `ComplaintRecord` and an
optional `NarrativeRecord`:

- `complaint_id` must be numeric, and `date_received` must parse (ISO date, ISO timestamp
  or `MM/DD/YYYY`). Otherwise `RecordError` is raised, and the row is counted and skipped.
- Text is stripped, and empty strings become NULL. Values are otherwise kept as published;
  business cleaning belongs in dbt staging.
- `record_hash` is the SHA-256 of the canonical business fields. Load metadata is
  excluded, so the same complaint from the API or the CSV hashes identically.

### 2.2 Idempotent merge (`loader.py`)

Every batch goes through the same path:

1. `COPY` the batch into a session temp table. There are no per-row round trips.
2. Merge the temp table into the target:

```sql
WITH src AS (SELECT DISTINCT ON (complaint_id) ... FROM tmp ORDER BY complaint_id),
merged AS (
  INSERT INTO raw.complaints AS t (...) SELECT ... FROM src
  ON CONFLICT (complaint_id) DO UPDATE SET ..., last_changed_at = now()
  WHERE t.record_hash IS DISTINCT FROM excluded.record_hash
  RETURNING (xmax = 0) AS inserted
)
SELECT count(*) FILTER (WHERE inserted), count(*) FILTER (WHERE NOT inserted), ...
```

- The `WHERE` on the conflict update skips unchanged rows entirely. A rerun writes nothing
  and creates no dead tuples, and `last_changed_at` moves only on a real change. That is
  the property every incremental dbt model depends on.
- `DISTINCT ON` protects against an id appearing twice in one batch, which would otherwise
  fail with "cannot affect row a second time".
- `xmax = 0` separates inserts from updates for the run statistics.
- Loader functions never commit; the pipeline owns the transaction boundary.

### 2.3 Sources and units of atomicity (`pipeline.py`)

| Source | Unit (commit and resume point) | Details |
|---|---|---|
| `api` | One date window (default 1 day) | Range = watermark − `INGEST_LOOKBACK_DAYS` (30) → today. Elasticsearch-backed, paged with `search_after` (sort values of the last hit), which stays stable while new complaints are indexed, unlike offsets. The window's rows and the watermark advance commit together. |
| `bulk` | One batch (10,000 rows) | Streams `complaints.csv.zip` without extracting it. 19M rows in one transaction would bloat WAL and block vacuum. Batches are idempotent, so a crash just reruns. |
| `narratives` | One archive file | The 21 FOIA files are catalogued with their coverage dates, so `--start/--end` picks only overlapping files. A file is recorded in `ops.ingested_files` with its SHA-256 and skipped when it is unchanged (`--force` overrides). |

- **Watermark.** `advance_watermark` only moves forward. A manual backfill of old dates
  never rewinds it.
- **HTTP** (`http.py`): retries transport errors, 429 and 5xx with bounded exponential
  backoff, and honours `Retry-After`. Downloads are cached under `data/`, resume with
  `Range`, and are skipped when the size and validators match the remote file.
- **Run log.** `load_run()` is a context manager. It writes `ops.load_runs` as `running`,
  then `succeeded` with counts, or `failed` with the error.

## 3. dbt warehouse

### 3.1 Staging

`stg_cfpb__complaints` is the only place source quirks are handled:

- **Sentinels for missing natural keys.** Missing values become `'Unknown'`, `'(none)'`,
  `'--'` or `'-----'`. Fact-to-dimension joins can then be equality hash joins.
  `IS NOT DISTINCT FROM` cannot be hashed, and on 19M rows that is the difference between
  seconds and hours.
- **Booleans.** `Yes` / `No` become booleans; anything else becomes NULL, never a guess.
- **ZIP masking.** ZIPs keep the CFPB masking (`391XX`). `zip3` and `is_zip_masked` are
  derived.

`stg_cfpb__narratives.narrative_clean` removes the redaction runs (`\mX{2,}(/X{2,})*\M`).
Without this, `XXXX` would be the most common "word" in the corpus, for both full text and
embeddings.

### 3.2 Keys

- **`hash_key(cols)`:** the first 16 hex digits of `md5(col1 | col2 | ...)`, cast to
  `bigint`. It is deterministic, so a dimension rebuild never renumbers keys that
  incremental facts point at. At 8 bytes it is much smaller than a 33-byte text MD5 on a
  19M-row fact and its indexes. The collision probability is about n²/2⁶⁵, roughly 1e-11
  for 20k dimension rows.
- **`date_key`:** a `YYYYMMDD` integer.
- **Unknown member:** `-1` in every dimension.

### 3.3 SCD Type 2 (ADR 0003)

`int_*_attributes` → snapshots (`check` strategy) → dimensions with half-open
`[valid_from, valid_to)` windows. The first version is backdated to 1900-01-01, and the key
is `hash(natural_key, valid_from)`. The fact joins "as was":

```sql
LEFT JOIN dim_company co
  ON c.company_name = co.company_name
 AND c.date_received >= co.valid_from AND c.date_received < co.valid_to
```

The generic test `scd2_valid_windows(natural_key)` fails on gaps, overlaps, or a natural
key without exactly one current row.

### 3.4 Partitioned fact (`partitioned_incremental` materialization)

dbt-postgres builds tables with `CREATE TABLE AS`, which cannot declare `PARTITION BY`. A
plain full refresh would also replace a partitioned table with an unpartitioned one. The
custom materialization works in four steps:

1. Stage the model's rows in a temp table.
2. On the first build, create the parent `LIKE <temp> PARTITION BY RANGE (date_received)`.
3. Create any missing monthly partitions for the months in the batch.
4. Delete and insert on `complaint_id`. Postgres routes the inserts to partitions.

The model uses `cw_is_incremental()`, because the built-in `is_incremental()` is true only
for `materialized='incremental'`.

- **Incremental filter.** Complaints whose staging row *or* narrative changed after
  `max(source_changed_at)` in the fact.
- **Indexes** (post-hooks): unique `(complaint_id, date_received)` (the partition key must
  be in a unique index), `(company_key, date_received)`, `(product_key, date_received)`,
  `(geo_key)` and `(source_changed_at)`, followed by `ANALYZE`.
- **Why monthly partitions:** dashboard and analytics queries filter on date ranges and
  prune to the months they need, and old months can be detached without rewriting the
  table.

### 3.5 Search tables

- **`complaint_text`:** incremental, delete+insert by `complaint_id`.
  - Chunking is done in SQL. `regexp_split_to_array` splits a narrative into words, and
    `generate_series` produces windows of 200 words that advance 160 words at a time.
    A narrative of n words gives `1 + ceil(max(n − 200, 0) / 160)` chunks.
  - Why chunk at all: a single vector for a 2,000-word complaint averages away the detail
    a query is looking for, and the model's 512-token limit would truncate it. 200 words is
    about 260–300 tokens. The 40-word overlap keeps a sentence that straddles a boundary
    whole in at least one chunk.
  - `tsv` is computed in the same model, so full text and vectors see identical text.
  - A changed narrative is deleted and reinserted with a NULL embedding, so a stale vector
    cannot survive.
  - `full_refresh = false` protects the embeddings.
  - Indexes: unique `(complaint_id, chunk_no)`, GIN `(tsv)`, and a partial index
    `WHERE embedding IS NULL` for the embedding job's keyset walk.
- **`complaint_facets`:** an unpartitioned table, one row per complaint with a narrative.
  It holds the filter and display attributes, with `product_family` from the current
  taxonomy. A filter probes one index here instead of 180 partition indexes.

### 3.6 Marts

These are materialized views that report "as is": they re-join the current dimension
version, so a reclassification restates history consistently. Timely rates are computed
over `responded` (`response_type <> 'In progress'`), because the CFPB publishes
in-progress complaints as timely. Two operational gotchas are covered in RUNBOOK §3:

- A table rebuild of a dimension cascades and drops the marts.
- dbt refreshes an existing materialized view instead of applying a changed query, so a
  changed query needs `--full-refresh`.

### 3.7 Tests

- **Generic tests** on every key and enum: `unique`, `not_null`, `relationships`,
  `accepted_values`, and `dbt_utils.unique_combination_of_columns`.
- **Singular tests:**
  - every staging row lands in the fact (row-count parity);
  - every product is mapped to a family;
  - unknown-member facts stay at or below 0.1% (warn level).
- **SCD2:** `scd2_valid_windows`.
- **Source freshness:** `raw.complaints` warns after 3 days and errors after 10.

## 4. Embedding job (ADR 0006)

```
loop:
  fetch   fetch_size (2048) chunks WHERE embedding IS NULL AND (complaint_id, chunk_no) > checkpoint
          ORDER BY complaint_id, chunk_no      -- keyset walk on the partial index; commit, release locks
  encode  bge-base-en-v1.5, batch 128, fp16 on CUDA, L2-normalised     -- no transaction open
  write   (writer thread, own connection)
          COPY (complaint_id, chunk_no, md5(text), vector) INTO temp
          UPDATE complaint_text t SET embedding, embedding_model, embedded_at
            FROM temp WHERE keys match AND t.embedding IS NULL
                        AND md5(t.narrative) = temp.md5                  -- stale-text guard
          UPSERT ops.embedding_checkpoints                               -- same transaction
```

- **Crash safety.** The vectors and the checkpoint commit together, so a crash loses at most
  one uncommitted batch.
- **Ordering.** A single writer thread keeps commits in key order while the GPU encodes the
  next batch. Overlapping the writes gave about 30% more throughput.
- **One model per column.** The job refuses to start if another model's vectors are present,
  until `embed reset` clears them.
- **End of a sweep.** The checkpoint becomes `complete` and the key resets. The next run
  picks up chunks dbt inserted since.
- **Queries vs documents.** Queries get the bge instruction prefix
  (`EMBEDDING_QUERY_INSTRUCTION`); documents do not.

## 5. Hybrid search function

`search.hybrid_search` is defined in `dbt/macros/search/hybrid_search.sql` and installed by
dbt's `on-run-end` hook. The hook skips it until both search tables exist, because a
`LANGUAGE sql` body is validated at `CREATE` time.

### Signature

```sql
search.hybrid_search(
  query_text text, query_embedding vector(768),
  match_count int = 10, candidate_count int = 100,
  keyword_weight float8 = 1.0, semantic_weight float8 = 1.0, rrf_k int = 60,
  date_from date = NULL, date_to date = NULL, product_family text = NULL,
  company_name text = NULL, state_code text = NULL,
  keyword_match text = 'all'              -- 'all' (AND) | 'any' (OR of the same lexemes)
) RETURNS TABLE (complaint_id bigint, score float8, keyword_rank int, semantic_rank int,
                 keyword_score real, cosine_similarity float8, best_chunk_no int)
```

### Query structure (CTEs)

| CTE | What it does | Why |
|---|---|---|
| `query` (NOT MATERIALIZED) | `websearch_to_tsquery`, with `' & '` replaced by `' \| '` for `any` | Once inlined it folds to a constant the planner can estimate. Materialized, it was assumed to match 0.5% of chunks, and an OR query ran for 471 s. |
| `allowed` (NOT MATERIALIZED) | complaint ids from `complaint_facets` that pass the filters | Each arm plans the facet lookup with indexes |
| `keyword_chunks` | top `candidate_count` chunks by `ts_rank_cd`, as an unfiltered and a filtered branch under `UNION ALL` | Constant guards mean exactly one branch runs. `no_filter OR id IN (...)` was never turned into a join and scanned every chunk (42 s); as a real join, the same query takes 1.05 s. |
| `keyword` | collapse to complaints (best chunk), `row_number()` → rank | A complaint ranks by its best chunk |
| `semantic_chunks` | top `candidate_count` by `embedding <=> q`, same two branches | The unfiltered branch keeps the `ORDER BY distance LIMIT n` shape an ANN index serves |
| `semantic` | collapse, `1 − min(distance)` as cosine similarity, rank | |
| `fused` | `FULL OUTER JOIN`; `score = w_kw/(k + r_kw) + w_sem/(k + r_sem)`, where a missing arm adds 0 | RRF: rank-only, no calibration, rewards agreement |

- **Inlining.** The function is `STABLE`, `LANGUAGE sql`, `PARALLEL SAFE`, with no `SET`
  clause, so Postgres inlines it. Parameters become constants:
  - a weight of 0 becomes `One-Time Filter: false`, and that arm never executes (this is how
    the keyword and semantic modes work);
  - NULL filters fold away.
- **Ties** break by `complaint_id` at every level, so results are deterministic. The
  benchmark's Python RRF reimplements this exactly and checks it against SQL.
- **Planned change (ADR 0004):** once the full-corpus HNSW index exists, the unfiltered
  semantic branch becomes `embedding::halfvec(768) <=> query_embedding::halfvec(768)`, and
  the pool sets `hnsw.ef_search = 200`.

## 6. API

`GET /search` parameters:

| Parameter | Default | Notes |
|---|---|---|
| `q` | required | Free text |
| `mode` | `hybrid` | `hybrid` (1/1), `keyword` (1/0), `semantic` (0/1) arm weights. Keyword mode never loads the model. |
| `keyword_match` | `all` | `any` ORs the terms. Opt-in, because it made hybrid worse (BENCHMARKS §3). |
| `k` | 10 | 1–100 complaints |
| `candidates` | 100 | Chunks per arm before fusion |
| `rrf_k` | 60 | |
| `date_from`, `date_to`, `product_family`, `company`, `state` | – | Applied inside each arm |

**Flow:**

1. Validate with pydantic. A failure returns 422 with the field errors, without echoing
   the input.
2. Embed the query, unless the mode is keyword.
3. Run one SQL statement: `hybrid_search(...)` joined to `complaint_facets` for display
   and to `complaint_text` for the best chunk. `ts_headline` builds the snippet when the
   keyword arm matched; otherwise the snippet is the first 300 characters.

Each hit returns its score, both arm ranks, the keyword score, the cosine similarity, the
complaint attributes, `chunk_no` and the snippet. The response also carries timings
(embed and SQL).

**Infrastructure:**

- **Connection pool.** `psycopg_pool` with `-c statement_timeout=30000` in the connection
  options. `QueryCanceled` becomes a 504 with a hint to add filters.
- **Model loading.** The model is loaded at startup (`SEARCH_PRELOAD_MODEL`), so the first
  request does not pay for it.
- **`/health`.** Reports the Postgres and pgvector versions, or 503 `degraded` when the
  database is unreachable.

## 7. Benchmark harness (`bench/`)

| Module | Role |
|---|---|
| `index_study.py` | `prepare` copies a deterministic 9% sample (`hashint8(complaint_id) & 1023 < 92`, so all chunks of a complaint go together) to `bench.chunk_vectors`, embeds 200 queries, and computes the ground truth. `index` builds one index at a time and records build time, size and pgvector notices. It sweeps `ef_search` / `probes` with a warm-up pass and a timed pass, checks the plan with `EXPLAIN`, and forces the index (`enable_seqscan = off`) when the planner refuses it, marking the point "forced". The study is resumable per index. |
| `knn.py` | Exact top-k computed client-side: streams vectors through a named binary cursor and merges per-batch top-k with numpy. One pass for all 200 queries took 66 s. |
| `quality.py` | 5 systems × 52 labelled queries through the real SQL function. Graded relevance comes from CFPB labels. Metrics are nDCG@10 (exponential gain), capped recall, MRR and latency. Includes a paired bootstrap and an RRF sweep computed in Python from one n = 200 candidate pull per arm; the run fails if the Python fusion disagrees with SQL. |
| `analytics.py` | `EXPLAIN (ANALYZE, TIMING OFF, BUFFERS, FORMAT JSON)` × 5 per query. Keeps the median and a text plan. |
| `metrics.py` | Pure functions with unit tests (`tests/test_bench.py`). |
| `report.py` | JSON → Markdown. Results are saved atomically after each unit of work. |

## 8. CI (`.github/workflows/ci.yml`)

| Job | Steps |
|---|---|
| `lint` | ruff, black `--check`, sqlfluff |
| `test` | Postgres + pgvector service; unit tests, then integration tests against the real database |
| `dbt` | Ingest a real CFPB fixture sample, run `dbt build` (full), run `dbt build` again (incremental, which must pass unchanged), `dbt docs generate`, embed the fixture chunks on CPU, check that an embedding rerun is a no-op, then run the end-to-end search tests with the real model |
| `docker` | Build the image |
