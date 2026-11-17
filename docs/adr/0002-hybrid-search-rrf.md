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
  keyword hits, and hybrid then degrades to semantic-only. Phase 6 measured OR matching
  (`keyword_match = 'any'`) against it; see the evaluation below. AND stays the default.
- The function is created by a dbt `on-run-end` hook, versioned with the tables it reads.
  `dbt run-operation install_search_functions` reinstalls it without a rebuild.

## Evaluation (phase 6)

`python -m complaint_warehouse.bench quality`. Full results are in
`bench/results/search_quality.md`.

- **Setup:** 52 queries written as a consumer would phrase them. Each is labelled with the
  CFPB issue and sub-issue it describes; the labels are the consumers' own choices, not my
  judgement of the results. The window is 2019: 124,843 complaints with narratives, and
  every arm is exact.

| system | nDCG@10 | recall@10 | MRR@10 | no results |
|---|---|---|---|---|
| keyword, AND | 0.149 | 0.104 | 0.363 | 21 of 52 |
| keyword, OR | 0.199 | 0.167 | 0.319 | 0 |
| semantic | 0.564 | 0.506 | 0.728 | 0 |
| hybrid, AND (default) | 0.563 | 0.496 | **0.801** | 0 |
| hybrid, OR | 0.442 | 0.392 | 0.598 | 0 |

What I take from it, with paired bootstrap 95% CIs on nDCG@10:

- **Hybrid with AND ties semantic on nDCG** (−0.0005, CI [−0.036, +0.035]). It puts the
  first relevant complaint higher (MRR 0.80 vs 0.73), and it costs nothing on this set. Each
  arm can only add candidates, and RRF promotes complaints both arms agree on.
- **OR matching makes hybrid worse** (−0.121, CI [−0.175, −0.065], 38 of 52 queries lose).
  An OR query matches about half of all chunks. `ts_rank_cd` then promotes chunks that repeat
  common words such as "account" or "money", and RRF gives that noise as much weight as the
  semantic ranks. OR helps keyword-only search a little (+0.049, CI includes 0) but not the
  fusion. It is also slow: it has to rank half the corpus.
- **RRF settings barely matter.** Across rrf_k ∈ {1…200} and keyword weights 0.25–2, hybrid
  with AND stays within 0.533–0.582 nDCG@10. The best cell (k = 200, keyword weight 0.5,
  0.582) is about 0.02 above the default. That is inside the noise of 52 queries and was
  chosen on those same queries, so I kept k = 60 and equal weights.
- **The query set favours semantic search.** The queries are paraphrases and the labels are
  topics, so this measures "find complaints about this kind of problem", which is semantic
  search's strength. Keyword search earns its place on exact terms (company names, product
  names, statutes, "Zelle"), which this set does not test. That is the main reason hybrid
  is the default rather than semantic-only.

The evaluation also exposed a planning bug. The tsquery CTE was materialized, so the planner
could not see it and assumed 0.5% of chunks match. For OR queries, which match about 50%, it
chose a GIN plan that ran up to 471 s with 11 GB of temp files. With `NOT MATERIALIZED`, the
immutable two-argument `websearch_to_tsquery` folds to a constant after inlining, and the
planner estimates from statistics. The same OR query filtered to 2019 then takes 5.4 s,
while AND queries keep their GIN plans.
