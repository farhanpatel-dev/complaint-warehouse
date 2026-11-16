# ADR 0002: Hybrid search with Reciprocal Rank Fusion, in one SQL function

- **Status:** Accepted
- **Date:** 2026-11-15
- **Deciders:** Farhan Patel

## Context

Complaint narratives are searched two ways, and each misses what the other finds:

- **Keyword (full-text):** precise on exact terms such as "escrow", "Zelle", a company name
  or a statute ("FCRA"). It misses paraphrases: "calling my workplace" does not match
  "phoned my place of employment".
- **Semantic (embeddings):** finds paraphrases and intent, but it can rank a generically
  similar complaint above one that contains the rare, exact term the user typed.

I wanted one search that does both, in Postgres, next to the data and its filters. A second
search system would have to be kept in sync.

## Decision

`search.hybrid_search(query_text, query_embedding, ...)` is a single `LANGUAGE sql` function
built from CTEs (`dbt/macros/search/hybrid_search.sql`):

1. **Keyword arm:** `websearch_to_tsquery` against the GIN-indexed `tsv`, ranked by
   `ts_rank_cd`, top `candidate_count` chunks.
2. **Semantic arm:** `ORDER BY embedding <=> query_embedding LIMIT candidate_count` (pgvector
   cosine distance).
3. Each arm collapses chunks to complaints (a complaint ranks by its best chunk) and numbers
   them 1..n.
4. **Fusion:** `score = w_kw / (k + rank_kw) + w_sem / (k + rank_sem)`, with k = 60. A complaint
   missing from an arm gets 0 from that arm.

### Why RRF rather than a weighted sum of scores

`ts_rank_cd` is unbounded and depends on document length. Cosine similarity is bounded but
its useful range depends on the model (bge scores bunch around 0.6–0.9). A weighted sum
needs both normalised, and min-max normalisation per query is unstable: one outlier
squashes everything else. RRF only uses ranks, so it needs no calibration, it is robust to
outliers, and it rewards agreement between the arms. k = 60 is the value from Cormack,
Clarke & Büttcher (SIGIR 2009). The labelled evaluation in phase 6 checks it, along with
the arm weights.

### Why in SQL rather than in the application

- One round trip, and filters apply inside each arm before its LIMIT, so a filtered search
  still returns a full set of candidates instead of "top 100, then filter down to 3".
- The same function serves the API, ad hoc SQL and the benchmark harness, so the evaluation
  measures exactly what users get.

### Choices that make it fast, and how I verified them

- **Inlinable.** STABLE, `LANGUAGE sql`, no `SET` clause, so Postgres inlines it into the
  caller. `EXPLAIN` shows no `Function Scan`. Parameters then become constants:
  - a weight of 0 turns an arm into `One-Time Filter: false`, and the arm is never executed;
  - NULL filters fold away, so an unfiltered search carries no filter at all.
- **Filtered and unfiltered branches** (`UNION ALL` with constant guards). My first version
  used `no_filter OR complaint_id IN (allowed)`. Postgres converts `IN (subquery)` to a
  semi-join *before* folding constants, and never under an `OR`, so a one-month,
  one-product filter still scanned every chunk: **42 s**. As an explicit join from
  `complaint_facets` the planner starts from the ~3k allowed complaints and probes chunks by
  primary key: **1.05 s** for the same query.
- **`search.complaint_facets`.** Filters need a lookup by `complaint_id`, but the fact table
  is partitioned by month, so that lookup would probe 180 partition indexes. A small
  unpartitioned table keyed on `complaint_id` does it in one probe.
- **Index-friendly semantic arm.** The unfiltered branch keeps the exact
  `ORDER BY embedding <=> q LIMIT n` shape an HNSW or IVFFlat index serves. Without an
  index that branch scans all 5.6M vectors (86–119 s), which is why phase 6 benchmarks the
  indexes.

## Measured (full corpus, no vector index yet)

| Query | Time |
|---|---|
| Keyword only, unfiltered ("charged twice for the same purchase") | 92 ms |
| Hybrid, one month + one product family | 1.05 s (was 42 s before the branch rewrite) |
| Hybrid or semantic, unfiltered | ~100 s exact scan, hits the API's 30 s timeout until phase 6 |

## Consequences

- The API's statement timeout (30 s, `SEARCH_STATEMENT_TIMEOUT_MS`) turns a too-broad
  search into a 504 with an explanation, instead of letting it tie up the database.
- `websearch_to_tsquery` ANDs every term, so long natural-language queries often get no
  keyword hits, and hybrid then degrades to semantic-only. The phase 6 evaluation measures
  this against OR semantics before I change it.
- The function is created by a dbt `on-run-end` hook, versioned with the tables it reads.
  `dbt run-operation install_search_functions` reinstalls it without a rebuild.
