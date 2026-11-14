# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added
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

### Changed
- The Postgres host port defaults to 5433 to avoid clashing with a local Postgres.
