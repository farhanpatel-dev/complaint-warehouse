SHELL := /bin/bash
.DEFAULT_GOAL := help

COMPOSE ?= docker compose
# GPU=1 runs batch jobs in the CUDA build of the image (docker-compose.gpu.yml).
ifeq ($(GPU),1)
  COMPOSE += -f docker-compose.yml -f docker-compose.gpu.yml
endif
RUN     := $(COMPOSE) run --rm worker

.PHONY: help up down reset logs psql install lint format test test-integration test-e2e ingest ingest-bulk ingest-narratives ingest-status dbt dbt-docs dbt-fresh embed embed-status bench

help: ## List available targets
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

# --- infrastructure ---------------------------------------------------------
up: ## Start Postgres+pgvector and the API
	$(COMPOSE) up -d --build --wait

down: ## Stop containers (keeps data volume)
	$(COMPOSE) down

reset: ## Stop containers and DELETE the data volume
	$(COMPOSE) down -v

logs: ## Tail container logs
	$(COMPOSE) logs -f

psql: ## Open a psql shell in the database container
	$(COMPOSE) exec db psql -U $${POSTGRES_USER:-cw} -d $${POSTGRES_DB:-complaints}

# --- local dev --------------------------------------------------------------
install: ## Install the package with dev extras and pre-commit hooks
	pip install -e ".[dev]"
	pre-commit install

lint: ## Run ruff, black --check and sqlfluff
	ruff check .
	black --check .
	sqlfluff lint db sql src dbt/models dbt/tests

format: ## Auto-format Python and SQL
	ruff check --fix .
	black .
	sqlfluff fix db sql src dbt/models dbt/tests

test: ## Run unit tests
	pytest -m "not integration and not e2e"

test-integration: ## Run tests that need the database (make up first)
	pytest -m integration

test-e2e: ## Run search tests against the built warehouse with the real model
	pytest -m e2e

# --- pipeline ---------------------------------------------------------------
ingest: ## Incremental API load from the watermark (ARGS="--start 2026-10-01" to override)
	$(RUN) python -m complaint_warehouse.ingest api $(ARGS)

ingest-bulk: ## Backfill raw.complaints from the bulk CSV (ARGS="--start 2023-01-01")
	$(RUN) python -m complaint_warehouse.ingest bulk $(ARGS)

ingest-narratives: ## Load narratives from the CFPB archive (ARGS="--start 2025-01-01")
	$(RUN) python -m complaint_warehouse.ingest narratives $(ARGS)

ingest-status: ## Show watermark, row counts and recent load runs
	$(RUN) python -m complaint_warehouse.ingest status

dbt: ## Seed, snapshot, build and test all dbt models
	$(RUN) bash -c "cd dbt && dbt deps --quiet && dbt build $(ARGS)"

dbt-fresh: ## Check source freshness of raw tables
	$(RUN) bash -c "cd dbt && dbt source freshness"

dbt-docs: ## Generate dbt docs and serve them on http://localhost:8081
	$(COMPOSE) run --rm -p 8081:8081 worker bash -c "cd dbt && dbt docs generate && dbt docs serve --host 0.0.0.0 --port 8081 --no-browser"

embed: ## Embed pending narrative chunks, resuming from the checkpoint (GPU=1, ARGS="--limit 20000")
	$(RUN) python -m complaint_warehouse.embed run $(ARGS)

embed-status: ## Show embedded/pending chunk counts and checkpoints
	$(RUN) python -m complaint_warehouse.embed status

bench: ## Run index and search-quality benchmarks (phase 6)
	$(RUN) python -m complaint_warehouse.bench $(ARGS)
