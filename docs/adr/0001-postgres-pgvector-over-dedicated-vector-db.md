# ADR 0001: PostgreSQL + pgvector instead of a dedicated vector database

- **Status:** Accepted
- **Date:** 2026-11-13 (written up 2026-11-17)
- **Deciders:** Farhan Patel

## Context

The project has two workloads over the same CFPB data:

- **Analytics:** a star schema over about 19.2M complaints, with dashboard aggregates and
  window-function queries (rolling rates, anomalies, cohorts).
- **Search:** keyword and semantic search over about 5.6M narrative chunks, filtered by the
  same attributes the analytics use: date, product family, company and state.

The usual architecture splits these: a warehouse for analytics, plus a vector database
(Pinecone, Qdrant, Weaviate, Milvus) or a search engine (Elasticsearch / OpenSearch) for
retrieval, kept in sync by a pipeline. I had to decide whether this project needed that
split.

My constraints:

- It runs on one laptop: Docker with 7.4 GB of RAM, a 16-thread CPU and an SSD.
- Everything is reproducible from `docker compose up`, with no paid services or API keys.
- Search filters must be exact and current. A result list that includes a complaint the
  filter excludes, or misses one because a copy is stale, is a bug.

## Decision

One PostgreSQL 16 instance with the pgvector extension (0.8.0) holds everything: raw
landing tables, the dbt star schema, the marts, the search chunks with their `tsvector` and
`vector(768)` columns, and the hybrid search function itself.

## Why

1. **Filters are joins, not a sync problem.** A filtered search joins
   `search.complaint_facets` to the chunks in the same query (ADR 0002). With a separate
   vector store, every filterable attribute has to be copied into its metadata and kept in
   sync. Pre-filter versus post-filter trade-offs then show up as recall bugs.
2. **Transactions cover the vectors.** The embedding job commits each batch of vectors and
   its checkpoint in one transaction, and only writes a vector if the chunk text is
   unchanged since it was read (ADR 0006). dbt replaces a changed narrative's chunks with
   delete+insert, so a stale vector cannot survive. Across two systems this would need
   outbox tables or reconciliation jobs.
3. **One query language for both workloads.** Keyword ranking (`ts_rank_cd` over a GIN
   index), vector distance and Reciprocal Rank Fusion fit in one inlinable SQL function.
   The API, ad hoc SQL and the benchmark harness all call that same function, so the
   evaluation measures exactly what users get.
4. **The scale fits.** 5.6M vectors of 768 dimensions is about 17 GB as float32 and about
   half that as `halfvec`. That is large for a laptop, but it is well within what a single
   Postgres instance handles. The Phase 6 index study measured HNSW on a 500k-chunk
   sample: 0.904 recall@100 at 17.5 ms p50 (ADR 0004). That is fast enough for an
   interactive API.
5. **Operational surface.** One database to back up, monitor, secure and run in CI. The CI
   job builds the whole stack, from ingestion through dbt and embeddings to end-to-end
   search, in one Postgres service container.

## Alternatives considered

| Option | Why not here |
|---|---|
| Dedicated vector DB (Qdrant, Weaviate, Milvus, Pinecone) | Faster ANN at large scale and built-in sharding, but every filter attribute must be duplicated into its metadata, and it adds a second consistency boundary. Pinecone is also a paid hosted service, which breaks "reproducible from compose". |
| Elasticsearch / OpenSearch | Strong BM25 and good kNN, but a JVM cluster next to the warehouse on a 7.4 GB laptop. Analytics would stay in Postgres anyway, so it would add a system rather than replace one. |
| DuckDB (+ vss extension) for everything | Excellent for the analytics scans, but it is an embedded single-writer engine. The API's concurrent reads alongside batch writes, and HNSW persistence (experimental in vss), made it a poor fit for the serving side. |
| FAISS in the API process | Fast and simple for a static index, but the index lives outside the database. Filters and updates become application code, and the index must be rebuilt or reloaded on every change. |

## Consequences

- **Index memory is the binding limit.** pgvector builds HNSW in `maintenance_work_mem`. The
  full-corpus index is estimated (by extrapolation, not measurement) at 9–10 GB, more than
  this laptop's Docker VM. ADR 0004 records the options. A dedicated vector DB would face
  the same memory need. It would just hide it on a managed cluster.
- **Postgres needs tuning for this workload.** Two settings mattered and were measured:
  `random_page_cost = 1.1` for the SSD (a one-year filter went from 64 s to 1.6 s), and
  `hnsw.ef_search`, which must be at least the candidate count or the semantic arm silently
  returns fewer rows (ADR 0004).
- **No horizontal scaling story built in.** Read replicas would scale search reads, and
  partitioning the fact already supports archiving old months. Sharding the vectors would
  need Citus or an application-level split.
- **When I would revisit this:** sustained query load beyond what replicas can serve; a
  corpus whose HNSW graph no longer fits in RAM on one machine even as `halfvec`; or a need
  for search features Postgres lacks, such as learned sparse retrieval or built-in
  rerankers. None of these applies to this project.
