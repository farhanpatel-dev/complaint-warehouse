-- Runs once, on first container start (empty data volume).
-- Extensions and schemas only; tables are owned by the ingestion job (raw) and dbt (everything else).

CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE EXTENSION IF NOT EXISTS pg_stat_statements;

-- Layered schemas mirror the data flow:
--   raw       -> landed exactly as the CFPB source delivers it
--   staging   -> typed / cleaned views built by dbt
--   warehouse -> star schema (facts + dimensions) built by dbt
--   marts     -> dashboard-facing aggregates and materialized views
--   search    -> chunked narratives, tsvector + embeddings, search functions
--   ops       -> pipeline bookkeeping (load watermarks, embedding checkpoints)
CREATE SCHEMA IF NOT EXISTS raw;
CREATE SCHEMA IF NOT EXISTS staging;
CREATE SCHEMA IF NOT EXISTS warehouse;
CREATE SCHEMA IF NOT EXISTS marts;
CREATE SCHEMA IF NOT EXISTS search;
CREATE SCHEMA IF NOT EXISTS ops;
