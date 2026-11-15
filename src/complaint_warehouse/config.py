"""Typed application settings loaded from environment variables (and an optional .env file)."""

from datetime import date
from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- database -------------------------------------------------------------
    database_url: str = Field(
        default="postgresql://cw:cw@localhost:5433/complaints",
        description="libpq connection string for the warehouse database.",
    )
    db_pool_min_size: int = Field(default=1, ge=0)
    db_pool_max_size: int = Field(default=10, ge=1)

    log_level: str = Field(default="INFO")

    # --- CFPB sources ---------------------------------------------------------
    cfpb_api_url: str = Field(
        default="https://www.consumerfinance.gov/data-research/consumer-complaints/search/api/v1/",
    )
    cfpb_bulk_url: str = Field(default="https://files.consumerfinance.gov/ccdb/complaints.csv.zip")
    cfpb_archive_base_url: str = Field(default="https://files.consumerfinance.gov/f/documents/")
    cfpb_user_agent: str = Field(default="complaint-warehouse/0.1 (python-httpx)")

    # --- ingestion behaviour --------------------------------------------------
    data_dir: Path = Field(default=Path("data"))
    ingest_start_date: date = Field(
        default=date(2025, 1, 1),
        description="Where the first incremental API load starts when no watermark exists.",
    )
    ingest_lookback_days: int = Field(
        default=30,
        ge=0,
        description="Re-read this many days behind the watermark to pick up late updates "
        "(company responses, timeliness) to complaints that were already loaded.",
    )
    ingest_window_days: int = Field(default=1, ge=1)
    ingest_batch_size: int = Field(default=10_000, ge=1)
    api_page_size: int = Field(default=1000, ge=1, le=10_000)
    http_timeout_seconds: float = Field(default=60.0, gt=0)
    http_max_retries: int = Field(default=5, ge=0)

    # --- embeddings -----------------------------------------------------------
    embedding_model: str = Field(
        default="BAAI/bge-base-en-v1.5",
        description="Sentence-transformers model id; must output embedding_dim dimensions.",
    )
    embedding_dim: int = Field(default=768, ge=1)
    embedding_device: str = Field(default="auto", description="auto, cpu, cuda or cuda:N")
    embedding_batch_size: int = Field(
        default=128, ge=1, description="Texts per forward pass on the device."
    )
    embedding_fetch_size: int = Field(
        default=2048, ge=1, description="Chunks read, embedded and committed per checkpoint."
    )
    embedding_max_seq_length: int = Field(default=512, ge=8)
    embedding_query_instruction: str = Field(
        default="Represent this sentence for searching relevant passages: ",
        description="Prefix bge models expect on queries (not on documents).",
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()
