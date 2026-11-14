SHELL := /bin/bash
.DEFAULT_GOAL := help

COMPOSE ?= docker compose
RUN     := $(COMPOSE) run --rm app

.PHONY: help up down reset logs psql install lint format test test-integration ingest ingest-bulk ingest-narratives ingest-status dbt embed bench

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
	sqlfluff lint db sql src

format: ## Auto-format Python and SQL
	ruff check --fix .
	black .
	sqlfluff fix db sql src

test: ## Run unit tests
	pytest -m "not integration"

test-integration: ## Run tests that need the database (make up first)
	pytest -m integration

# --- pipeline ---------------------------------------------------------------
ingest: ## Incremental API load from the watermark (ARGS="--start 2026-10-01" to override)
	$(RUN) python -m complaint_warehouse.ingest api $(ARGS)

ingest-bulk: ## Backfill raw.complaints from the bulk CSV (ARGS="--start 2023-01-01")
	$(RUN) python -m complaint_warehouse.ingest bulk $(ARGS)

ingest-narratives: ## Load narratives from the CFPB archive (ARGS="--start 2025-01-01")
	$(RUN) python -m complaint_warehouse.ingest narratives $(ARGS)

ingest-status: ## Show watermark, row counts and recent load runs
	$(RUN) python -m complaint_warehouse.ingest status

dbt: ## Build and test dbt models (phase 3)
	$(RUN) bash -c "cd dbt && dbt deps && dbt build"

embed: ## Chunk and embed complaint narratives (phase 4)
	$(RUN) python -m complaint_warehouse.embed $(ARGS)

bench: ## Run index and search-quality benchmarks (phase 6)
	$(RUN) python -m complaint_warehouse.bench $(ARGS)
