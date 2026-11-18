# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [Unreleased]

## [0.1.0] - 2026-11-17

### Added
- Documentation: HLD, LLD, data dictionary, runbook, benchmarks summary and interview notes
  under `docs/`, plus ADR 0001 (Postgres + pgvector over a dedicated vector database) and
  ADR 0003 (SCD Type 2 with dbt snapshots).
- Benchmark harness (`python -m complaint_warehouse.bench`, `make bench ARGS=...`): vector index
  study (HNSW / IVFFlat, `vector` / `halfvec`, exact baseline, client-side exact ground truth),
  GIN keyword study, labelled search-quality evaluation (nDCG, capped recall, MRR, paired
  bootstrap, RRF sensitivity) and analytics timings; JSON results with generated Markdown.
- 52 labelled evaluation queries (`bench/queries/eval_queries.toml`) graded by CFPB issue and
  sub-issue labels.
- Analytics SQL library (`sql/analytics/`): rolling 30-day timely rate, z-score anomalies,
  Wilson-ranked timely-response league table, company cohorts, YoY and YTD trends.
- `keyword_match` (`all` | `any`) parameter on `search.hybrid_search` and `GET /search`.

- `search.hybrid_search` SQL function: keyword (`ts_rank_cd`) and semantic (pgvector cosine)
  arms fused with Reciprocal Rank Fusion; mode weights, in-arm filters, inlinable, created by
  a dbt `on-run-end` hook (`dbt run-operation install_search_functions` to reinstall).
- `search.complaint_facets` dbt model: unpartitioned per-complaint lookup for filters and display.
- `GET /search` FastAPI endpoint (hybrid / keyword / semantic, filters, snippets, timings),
  a statement timeout that maps to 504, and model preloading at startup.
- End-to-end search tests (`pytest -m e2e`); CI now embeds the fixture sample on CPU and runs them.
- ADR 0002: hybrid search with RRF, with measured query plans.
- Embedding job (`python -m complaint_warehouse.embed`) using `BAAI/bge-base-en-v1.5`: keyset
  walk over pending chunks, a checkpoint committed atomically with each batch, writes overlapped
  with GPU encoding, a stale-text guard, and a single-model-per-column safety check.
- `ops.embedding_checkpoints` table, plus `embed status` and `embed reset` commands.
- CUDA 12.8 build of the image via `docker-compose.gpu.yml` (`make embed GPU=1`); the default
  image ships CPU torch.
- ADR 0006: embedding model choice and batch job design, with measured throughput.
- dbt project with staging views, a star schema (`fact_complaint`, `dim_company`, `dim_product`,
  `dim_date`, `dim_geo`), `search.complaint_text` chunks and four dashboard materialized views.
- `partitioned_incremental` custom materialization: monthly range-partitioned fact table with
  automatic partition creation and delete+insert loads.
- SCD Type 2 for companies and products using dbt snapshots over curated reference seeds,
  plus an `scd2_valid_windows` generic test (no gaps or overlaps, one current version).
- Deterministic 64-bit hash surrogate keys and unknown-member rows in every dimension.
- dbt tests (unique, not_null, relationships, accepted_values, singular row-count and
  unknown-member checks), source freshness, and dbt docs.
- CI job that ingests a real CFPB fixture sample and runs `dbt build` twice (full, then incremental).
- Ingestion job (`python -m complaint_warehouse.ingest`) with three CFPB sources: incremental
  search-API loads (per-day windows, `search_after` paging, watermark with 30-day lookback),
  bulk CSV backfill, and the FOIA Narratives Archive.
- `raw.complaints`, `raw.complaint_narratives`, and the `ops.load_runs`, `ops.watermarks` and
  `ops.ingested_files` bookkeeping tables.
- Idempotent COPY + hash-guarded upsert loader. Unchanged rows are never rewritten.
- Download cache with Range resume, and HTTP retries with exponential backoff and Retry-After.
- ADR 0005: narratives sourced from the CFPB FOIA archive after the CFPB stopped publishing them.
- Repository skeleton: `src/` package layout, typed settings, Postgres connection helpers.
- Docker Compose stack with Postgres 16 + pgvector 0.8 and a FastAPI app container.
- Database bootstrap script creating the `vector`, `pg_trgm` and `pg_stat_statements`
  extensions and the `raw`, `staging`, `warehouse`, `marts`, `search` and `ops` schemas.
- `/health` endpoint reporting Postgres and pgvector versions.
- Makefile, `.env.example`, pre-commit (ruff, black, sqlfluff), pytest suite and GitHub Actions CI.

### Fixed
- Timely rates in the marts and analytics queries now exclude complaints still "In progress".
  The CFPB publishes them as timely before the company answers (651k complaints, 74% of the
  current month), which inflated recent months. The marts gain a `responded` column.

### Changed
- Postgres `random_page_cost` set to 1.1 (SSD): a search filtered to one year chose a 64 s
  sequential scan over a 1.6 s index nested loop with the default of 4.
- The tsquery CTE in `search.hybrid_search` is `NOT MATERIALIZED`, so the planner estimates
  matches from statistics. An OR query had run for up to 471 s on a 0.5% default estimate.
- Postgres `shm_size` raised to 3 GB so parallel index builds can use 2 GB of
  `maintenance_work_mem`.
- The Postgres host port defaults to 5433 to avoid clashing with a local Postgres.
