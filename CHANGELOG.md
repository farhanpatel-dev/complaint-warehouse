# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added
- Repository skeleton: `src/` package layout, typed settings, Postgres connection helpers.
- Docker Compose stack with Postgres 16 + pgvector 0.8 and a FastAPI app container.
- Database bootstrap script creating the `vector`, `pg_trgm` and `pg_stat_statements`
  extensions and the `raw`, `staging`, `warehouse`, `marts`, `search` and `ops` schemas.
- `/health` endpoint reporting Postgres and pgvector versions.
- Makefile, `.env.example`, pre-commit (ruff, black, sqlfluff), pytest suite and GitHub Actions CI.
