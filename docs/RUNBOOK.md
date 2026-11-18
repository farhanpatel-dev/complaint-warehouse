# Runbook

How to build, run, check and repair the warehouse. Every command runs from the repo root.
Timings are from my laptop (16 threads, SSD, Docker VM with 7.4 GB RAM, RTX 5050 laptop
GPU) on the full dataset.

> **Windows / Git Bash:** prefix `docker compose run ... worker <cmd>` with
> `MSYS_NO_PATHCONV=1` whenever an argument is a container path (`/app/...`). Otherwise
> Git Bash rewrites it into a Windows path. `make` targets are not affected.

## 1. First full build

| Step | Command | Time (measured) |
|---|---|---|
| Start Postgres and the API | `cp .env.example .env && make up` | < 1 min |
| Backfill all complaints | `make ingest-bulk` | 32 min for 19.17M rows (plus the download) |
| Catch up from the API | `make ingest` | about 3 min for 151k rows; a rerun takes 16 s and writes 0 rows |
| Load narratives | `make ingest-narratives` | about 20 min for 21 archive files, 3.85M narratives |
| Build the warehouse | `make dbt` | about 12 min (seeds, snapshots, 19M-row fact, chunks, marts, 91 tests) |
| Embed chunks | `make embed GPU=1` | 4 h 03 min for 5.5M chunks (376 chunks/s). CPU is far slower. |
| Reclaim the update bloat | `VACUUM (ANALYZE) search.complaint_text;` in `make psql` | 3.5 min |

Total disk use afterwards is 52 GB: `search.complaint_text` is 32 GB, `raw.complaints`
8.7 GB and the warehouse tables 3.5 GB. Leave at least 70 GB free for the build.

## 2. Routine refresh

Run these in order. Each step is idempotent, so rerunning one is always safe.

```bash
make ingest          # API from the watermark minus 30 days (late company responses)
make dbt             # incremental: fact, chunks and snapshots process only changed rows
make embed GPU=1     # embeds only chunks dbt inserted or re-chunked (NULL embedding)
```

The narrative archive is frozen (ADR 0005), so `ingest-narratives` is a one-off.

### Health checks

| Check | Command | Healthy |
|---|---|---|
| API and versions | `curl localhost:8000/health` | `{"status":"ok", ...}` |
| Loads | `make ingest-status` | watermark ≈ yesterday; last runs `succeeded` |
| Source freshness | `make dbt-fresh` | `raw.complaints` within 3 days (warn) / 10 days (error) |
| Embeddings | `make embed-status` | pending 0; checkpoint `complete` |
| dbt tests | `make dbt` | 0 errors. One warn-level test flags > 0.1% unknown-member facts. |

```sql
-- Failed or stuck runs
SELECT load_id, source, status, started_at, error
FROM ops.load_runs WHERE status <> 'succeeded' ORDER BY load_id DESC;

-- Facts that missed a dimension (should be 0)
SELECT count(*) FILTER (WHERE company_key = -1) AS company,
       count(*) FILTER (WHERE product_key = -1) AS product,
       count(*) FILTER (WHERE geo_key = -1)     AS geo
FROM warehouse.fact_complaint;
```

## 3. Known problems and fixes

### Marts disappear after a dimension is rebuilt

**Symptom:** `relation "marts.mv_daily_volume" does not exist`, or analytics queries fail
after a `dbt build --select dim_company` (or any selection that rebuilds a dimension
without the marts).

**Cause:** dbt rebuilds a `table` model by creating a new table and dropping the old one
with `CASCADE`. The materialized views depend on the dimensions, so Postgres drops them
too. It happened to me in phase 6: `mv_daily_volume` and `mv_company_monthly` vanished.

**Fix:**

```bash
make dbt ARGS="--select marts"
```

**Prevention:** when selecting a dimension, include its dependants:
`--select dim_company+`.

### A changed mart query is not applied

**Symptom:** you edit a model in `dbt/models/marts/`, run `dbt build`, and the log says
`REFRESH MATERIALIZED VIEW`. The new column is missing.

**Cause:** for an existing materialized view, dbt-postgres only compares configuration
(indexes). It does not compare the SQL. It refreshes the old definition. I hit this in
phase 7 when I added `responded` to the marts.

**Fix:**

```bash
make dbt ARGS="--select marts --full-refresh"
```

This took 2 minutes for all four marts.

### `search.hybrid_search` is missing or stale

The function is created by the dbt `on-run-end` hook, and only once both search tables
exist. To reinstall it without a build:

```bash
docker compose run --rm worker bash -c "cd dbt && dbt run-operation install_search_functions"
```

### Search returns 504

The API's statement timeout is 30 s (`SEARCH_STATEMENT_TIMEOUT_MS`). In order of
likelihood:

1. **Unfiltered semantic or hybrid search with no vector index.** This is expected until
   the full-corpus HNSW index exists (ADR 0004). The exact scan of 5.6M vectors takes
   86–119 s. Add a filter (date range or product family), or use `mode=keyword`.
2. **`keyword_match=any` over a broad window.** An OR query matches about half the corpus.
   This is why it is opt-in.
3. **Bad plan.** Run the query under `EXPLAIN (ANALYZE, BUFFERS)` in `make psql` and check:
   - `SHOW random_page_cost;` must be 1.1. With 4, a one-year filter took a 64 s sequential
     scan instead of a 1.6 s index nested loop.
   - The plan has no `Function Scan` on `hybrid_search`. If it has one, inlining broke, for
     example because someone added a `SET` clause to the function.

### Vector index: too few results, or a build that never finishes

- **HNSW returns at most `hnsw.ef_search` rows (default 40).** With `candidate_count = 100`,
  set `hnsw.ef_search = 200` on the session or pool. Never set it on the function: that
  stops inlining.
- **Builds need `maintenance_work_mem` larger than the graph.** Otherwise pgvector falls
  back to a much slower on-disk build and logs a notice. The 500k-sample halfvec m32 index
  is 858 MB. The full corpus is estimated at 9–10 GB, more than this Docker VM has.
- **Parallel builds put `maintenance_work_mem` in `/dev/shm`.** `shm_size` (3 GB in
  compose) must stay above the largest `maintenance_work_mem` a build sets (2 GB in the
  benchmark).

### Running out of memory

The Docker VM has 7.4 GB, and Postgres alone uses about 3 GB under load (512 MB
`shared_buffers`, plus `work_mem` and `maintenance_work_mem` per operation). I have had
long jobs killed for low memory during the heavy phases. To avoid it:

- Run one heavy job at a time: embed, a full `dbt build`, a benchmark `index` run, or a
  vector index build.
- Stop the API (`docker compose stop app`) during index builds.
- Every job resumes safely after a kill:
  - ingestion is idempotent per window, batch or file;
  - embedding resumes from its checkpoint;
  - `bench index` skips finished indexes.

### A load failed halfway

Rerun the same command. Each source has its own unit of atomicity:

- **API:** a date window commits with its watermark.
- **Bulk:** a batch.
- **Narratives:** a file, skipped by SHA-256 once fully loaded.

A failed run is marked `failed` in `ops.load_runs`, with the error. To reload an old range
without moving the watermark backwards, use `make ingest ARGS="--start 2026-01-01"`. The
watermark only moves forward.

### Bloat after an embedding pass

Each embedded row is updated once, which leaves a dead tuple. After the full run, 0.97M
dead tuples remained, about 3 KB of TOAST each.

```sql
SELECT n_live_tup, n_dead_tup, last_autovacuum
FROM pg_stat_user_tables WHERE relname = 'complaint_text';
VACUUM (ANALYZE) search.complaint_text;   -- 3.5 min after the full run
```

## 4. Changes that need care

### Reclassifying a company or product (SCD2)

1. Edit `dbt/seeds/company_groups.csv` or `product_taxonomy.csv`.
2. Run `make dbt`. The snapshot closes the old version and opens a new one, the fact keeps
   its as-was keys, and the marts restate history under the new classification.
3. `scd2_valid_windows` must pass (no gaps, no overlaps, one current version).

Never delete rows from `snapshots.*`. They are the only record of past versions and
cannot be rebuilt from source.

### Changing the embedding model

Vectors from two models are not comparable, and the job refuses to mix them.

1. Set `EMBEDDING_MODEL` (and `EMBEDDING_DIM`, plus `embedding_dim` in `dbt_project.yml`
   if the dimension changes).
2. Run `docker compose run --rm worker python -m complaint_warehouse.embed reset --yes`.
3. Run `make embed GPU=1`. Budget about 4 hours on the GPU.

The API must embed queries with the same model, so restart it afterwards.

### Full refreshes

- `search.complaint_text` has `full_refresh = false`: `--full-refresh` leaves it, and its
  embeddings, alone. To re-chunk on purpose, drop the table, rebuild, then re-embed.
- `fact_complaint` can be fully refreshed from raw. It took 319 s of the 12-minute first build.

## 5. Backup and restore

The expensive state is the embeddings (4 GPU hours) and the snapshots (not rebuildable).
Everything else can be rebuilt from the CFPB sources in roughly an hour plus downloads.

```bash
# Backup: custom format, compressed
docker exec cw-db pg_dump -U cw -d complaints -Fc -f /tmp/complaints.dump
docker cp cw-db:/tmp/complaints.dump ./complaints.dump

# Minimal backup of state that cannot be rebuilt cheaply
docker exec cw-db pg_dump -U cw -d complaints -Fc -n snapshots -n ops -t search.complaint_text -f /tmp/state.dump

# Restore into a fresh volume
make reset && make up
docker cp ./complaints.dump cw-db:/tmp/complaints.dump
docker exec cw-db pg_restore -U cw -d complaints --no-owner -j 4 /tmp/complaints.dump
```

## 6. Running the benchmarks

See [`bench/README.md`](../bench/README.md) and [BENCHMARKS.md](BENCHMARKS.md). In short:

```bash
make bench ARGS=prepare     # ~10 min: 9% sample, query embeddings, exact ground truth
make bench ARGS=index       # 7 indexes, resumable; the builds alone take 26 min
make bench ARGS="gin"
make bench ARGS="quality"   # 52 queries x 5 systems on the 2019 window
make bench ARGS="analytics"
make bench ARGS="report"    # regenerate Markdown from the JSON results
```

To test a code change without rebuilding the image, mount `src` into a detached worker:

```bash
MSYS_NO_PATHCONV=1 docker compose run -d --name bench-run \
  -v "$PWD/src:/app/src" -e PYTHONPATH=/app/src \
  worker python -m complaint_warehouse.bench quality
docker logs -f bench-run
```
