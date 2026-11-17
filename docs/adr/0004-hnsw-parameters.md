# ADR 0004: HNSW on halfvec, m = 32, ef_construction = 128, ef_search ≥ 2 × candidates

- **Status:** Accepted for the parameters. The full-corpus build is pending (see Consequences).
- **Date:** 2026-11-17
- **Deciders:** Farhan Patel

## Context

Without a vector index, the unfiltered semantic arm of `search.hybrid_search` compares the
query against all 5.6M chunk vectors. That takes 86–119 s, far over the API's 30 s timeout.
pgvector offers two approximate indexes: HNSW (a navigable graph) and IVFFlat (k-means lists).
Each can index either the stored float32 `vector` or a `halfvec` (float16) expression. I
wanted to choose between them on measurements from this corpus and this model, not on
defaults or blog numbers.

## How I measured

`python -m complaint_warehouse.bench prepare|index` (method in `bench/README.md`, all numbers
in `bench/results/index_study.md`):

- **Data:** 501,685 chunks from 345,351 complaints, a 9% hash sample by complaint, so every
  chunk of a sampled complaint is kept.
- **Queries:** 200 in total: the 52 written evaluation queries plus 148 CFPB issue and
  sub-issue names, all embedded with the bge query prefix.
- **Ground truth:** the exact top-100 per query, computed client-side in one 66 s pass
  (`bench/knn.py`). The Postgres exact scan agreed with it (recall 1.000).
- **Procedure:** one index at a time; build time and size recorded, then a sweep of
  `ef_search` or `probes`, with a warm-up pass and then a timed pass. Recall was measured at
  LIMIT 10 and LIMIT 100, because hybrid search asks each arm for 100 candidates. The
  harness fails a point if `EXPLAIN` shows the index was not used.

## Results (500k sample)

| index | build | size | recall@10 / p50 | recall@100 / p50 |
|---|---|---|---|---|
| exact scan | – | – | 1.000 / 1,080 ms | 1.000 / 1,099 ms |
| HNSW vector m16 efc64, ef 100 | 285 s | 1,637 MB | 0.831 / 7.2 ms | 0.736 / 8.9 ms |
| HNSW halfvec m16 efc64, ef 100 | 157 s | 836 MB | 0.821 / 5.2 ms | 0.733 / 7.5 ms |
| HNSW vector m32 efc128, ef 200 | 488 s | 1,616 MB | 0.948 / 20.6 ms | 0.910 / 22.3 ms |
| **HNSW halfvec m32 efc128, ef 200** | **381 s** | **858 MB** | **0.940 / 15.3 ms** | **0.904 / 17.5 ms** |
| IVFFlat vector 501 lists, probes 20 | 159 s | 1,963 MB | 0.812 / 23.8 ms | 0.751 / 28.9 ms |
| IVFFlat halfvec 501 lists, probes 50 | 88 s | 786 MB | 0.894 / 33.6 ms | 0.871 / 36.1 ms |

## Decision

1. **HNSW, not IVFFlat.** HNSW is faster at every recall level I measured. For about 0.9
   recall@10, HNSW halfvec m32 needs 9.8 ms (ef 100). IVFFlat needs 66 ms (float32, probes 50)
   or 34 ms (halfvec, probes 50), and its p95 is 4.7–9 times higher (72 or 144 ms, against
   15.4 ms). IVFFlat's only
   advantage is build time, and the lists go stale as data is added; HNSW handles inserts.
2. **halfvec.** At matched settings with `ef_search` of 20 or more, it loses at most 0.013
   recall (0.03 at ef 10, a setting I would not use). In exchange the index is
   half the size (858 MB vs 1,616 MB), builds 22–45% faster, and queries are 20–30% faster
   because fewer pages are read. The table keeps float32. The index is an expression index
   on `embedding::halfvec(768)`, so the query has to use the same expression.
3. **m = 32, ef_construction = 128.** Hybrid search consumes the top 100 from each arm, so
   recall@100 is what counts.
   - **Recall per millisecond:** m32 at ef 200 gives 0.904 recall@100 in 17.5 ms. m16 at
     ef 400 gives 0.877 in 19.8 ms. The denser graph wins at the same latency.
   - **Cost:** 2.4 times the build time, at almost the same size. At 768 dimensions the
     vectors dominate the pages, and the larger neighbour lists fit into space that was
     already left over.
4. **`hnsw.ef_search` = 200, set per session.** HNSW returns at most `ef_search` rows, and
   pgvector's default is 40. With `candidate_count` = 100, the semantic arm would quietly
   return only 40 candidates. `ef_search` must be at least `candidate_count`, and 2× buys
   recall at a known cost. It can't be a `SET` on the function, because that stops inlining
   (ADR 0002). It goes on the API's connection options instead, next to `statement_timeout`.

## Why recall is lower than published benchmarks

At pgvector's defaults (m16, ef 40) recall@10 is 0.71, below figures often quoted for HNSW.
I think two properties of this workload explain it, though I have not separated their
effects:

- **Asymmetric queries.** Queries are short questions embedded with an instruction prefix;
  the indexed vectors are 150-word passages. Queries from a different distribution than the
  data are known to be harder for graph search, which follows neighbourhoods built from the
  data.
- **Near-duplicates.** Overlapping chunk windows and repeated complaint templates put many
  vectors almost the same distance from a query. When the 10th and 11th neighbours are
  separated by a tiny distance, a swap counts as a miss even though the result is just as
  good.

That is why I chose by recall@100 at a fixed latency, and why the parameters have to be
checked on the full corpus.

## Two planner findings from the study

- **IVFFlat with many probes loses the index.** At `probes = 50` (10% of the lists), the
  planner chose a sequential scan for the float32 IVFFlat. The run used the default
  `random_page_cost = 4`. A high `probes` setting can silently turn into a full scan.
  The benchmark records this and forces the index (marked "forced" in the results).
- **`random_page_cost` was wrong for this disk.** With the default 4.0, a semantic search
  filtered to one year (131k complaints) picked a parallel sequential scan over all
  5.6M chunks: **64 s**. The index nested loop takes 1.6 s, and 1.4 s warm. Setting
  `random_page_cost = 1.1`, the usual value for SSDs, makes the planner choose the nested
  loop on its own. It is now set in `docker-compose.yml`. The search-quality evaluation ran
  with it. The vector-index sweep ran before the change, but its queries use only the HNSW
  or IVFFlat index, apart from the forced IVFFlat point above.

## Consequences

- **Full-corpus build, pending.** pgvector builds the HNSW graph in `maintenance_work_mem`.
  Scaling the sample's 858 MB linearly to 5.6M rows gives roughly 9–10 GB. That is an
  extrapolation, not a measurement, and it is more than this laptop's Docker VM (7.4 GB).
  The build would fall back to pgvector's much slower on-disk mode. Options:
  - build it on a larger machine and restore it;
  - accept a long on-disk build;
  - index a subset, such as recent years, with a partial index.

  Recall at 5.6M points will also be lower at the same `ef_search` and must be re-measured
  with `bench prepare --sample-pct 100`.
- **Function change.** `search.hybrid_search` needs its unfiltered semantic branch rewritten
  to `embedding::halfvec(768) <=> query_embedding::halfvec(768)`. It is not rewritten yet:
  until the full-corpus index exists, the rewrite would only cost a cast per row.
- **Filtered searches** keep the exact path (join from `complaint_facets`, then compute
  distances), which is fast for selective filters. For broad filters, an alternative is the
  HNSW index plus pgvector 0.8's `hnsw.iterative_scan`, which post-filters the index scan.
  That trade-off needs its own measurement.
