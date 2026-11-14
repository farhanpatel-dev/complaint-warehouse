FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

# Install dependencies first so code changes don't bust the layer cache.
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install ".[dbt]"

COPY . .

# dbt packages are resolved at build time so `make dbt` works offline.
RUN cd dbt && DBT_PROFILES_DIR=/app/dbt dbt deps --quiet

EXPOSE 8000

# The healthcheck lives on the `app` service in docker-compose.yml, not here: one-off job
# containers from this image (ingest, dbt, embed) run no web server.
CMD ["uvicorn", "complaint_warehouse.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
