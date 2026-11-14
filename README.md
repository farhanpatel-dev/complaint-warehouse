# complaint-warehouse

A PostgreSQL + pgvector warehouse over the CFPB Consumer Complaint Database that supports both analytics and hybrid (keyword + semantic) search.

> Work in progress. I am building this in phases; see `CHANGELOG.md` for what has landed.

## Quickstart (current state)

```bash
cp .env.example .env
make up                       # or: docker compose up -d --build --wait
curl localhost:8000/health    # {"status":"ok", "database": {"postgres": "16.x", "pgvector": "0.8.0"}}
```

Local development:

```bash
python -m venv .venv && source .venv/bin/activate
make install                  # pip install -e ".[dev]" + pre-commit hooks
make lint test
```

## Repository layout

```
src/complaint_warehouse/   Python package (config, db helpers, API; ingest/embed/bench in later phases)
db/init/                   SQL run on first Postgres start (extensions + schemas)
dbt/                       dbt project (phase 3)
sql/                       analytics SQL library (phase 6)
bench/                     benchmark harness and labeled query set (phase 6)
docs/                      HLD, LLD, ADRs, runbook, benchmarks
tests/                     pytest suite
```

## License

MIT © 2026 Farhan Patel
