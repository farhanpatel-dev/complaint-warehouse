# Benchmarks

A summary of everything I measured, with the decisions each result drove. Every number
here is copied from the generated reports in [`bench/results/`](../bench/results/). Those
reports are rendered from the raw JSON by `make bench ARGS=report`, so nothing is typed in
by hand. The method is in [`bench/README.md`](../bench/README.md).

**Environment:** PostgreSQL 16.10, pgvector 0.8.0, `shared_buffers` 512 MB, `work_mem`
64 MB, 2 parallel workers per gather, JIT on. The Docker VM has 16 CPUs and 7.4 GB RAM on a
laptop SSD. Latencies are warm-cache and measured at the client unless stated otherwise.
This is one machine, so read the numbers as relative, not as capacity planning.

## 1. Vector indexes: HNSW vs IVFFlat, `vector` vs `halfvec`

**Question:** which approximate index serves the semantic arm, which needs the top 100
candidates in well under the API's 30 s timeout?

**Setup:**
- 501,685 chunks: a 9% hash sample of complaints, keeping all their chunks.
- 200 queries: 52 written evaluation queries plus 148 CFPB issue names.
- Ground truth: the exact top-100 per query, computed client-side. Postgres's exact scan
  agreed with it at recall 1.000.
- Each index was built alone, then swept over `ef_search` / `probes`.
- `EXPLAIN` checks that the index is used. Where the planner refused it, the index was
  forced and the point marked.

### Build cost

| index | build | size |
|---|---|---|
| HNSW `vector` m16 efc64 | 284.9 s | 1,637 MB |
| HNSW `vector` m32 efc128 | 487.6 s | 1,616 MB |
| IVFFlat `vector` 501 lists | 158.5 s | 1,963 MB |
| HNSW `halfvec` m16 efc64 | 157.0 s | 836 MB |
| **HNSW `halfvec` m32 efc128** | **380.7 s** | **858 MB** |
| IVFFlat `halfvec` 501 lists | 87.8 s | 786 MB |

### Recall vs latency

| index | setting | recall@10 | p50 / p95 ms | recall@100 | p50 / p95 ms |
|---|---|---|---|---|---|
| exact scan | – | 1.000 | 1,080 / 1,152 | 1.000 | 1,099 / 1,185 |
| HNSW `vector` m16 | ef 40 (pgvector default) | 0.714 | 3.8 / 6.1 | – | – |
| HNSW `vector` m16 | ef 400 | 0.921 | 21.4 / 31.2 | 0.883 | 23.5 / 33.3 |
| HNSW `halfvec` m16 | ef 400 | 0.912 | 15.4 / 24.1 | 0.877 | 19.8 / 29.8 |
| HNSW `vector` m32 | ef 200 | 0.948 | 20.6 / 31.8 | 0.910 | 22.3 / 31.5 |
| HNSW `halfvec` m32 | ef 100 | 0.906 | 9.8 / 15.4 | 0.845 | 11.5 / 16.6 |
| **HNSW `halfvec` m32** | **ef 200** | **0.940** | **15.3 / 22.5** | **0.904** | **17.5 / 26.0** |
| IVFFlat `vector` | probes 50 (forced) | 0.913 | 66.1 / 144.2 | 0.879 | 68.4 / 148.9 |
| IVFFlat `halfvec` | probes 50 | 0.894 | 33.6 / 72.0 | 0.871 | 36.1 / 70.9 |

**Findings:**

- **HNSW beats IVFFlat at every recall level.** At about 0.9 recall@10, HNSW `halfvec` takes
  9.8 ms. IVFFlat takes 34–66 ms, with a p95 4.7–9× worse.
- **`halfvec` is nearly free.** It costs at most 0.013 recall at `ef_search` ≥ 20. In return
  the index is half the size, builds 22–45% faster and queries 20–30% faster.
- **m32 beats m16 at equal latency.** m32 at ef 200 gives 0.904 recall@100 in 17.5 ms; m16
  at ef 400 gives 0.877 in 19.8 ms.
- **The pgvector default `ef_search` = 40 is a trap.** HNSW returns at most `ef_search`
  rows, so a LIMIT 100 query would get 40.

**Decision:** HNSW on `embedding::halfvec(768)`, m = 32, ef_construction = 128,
`hnsw.ef_search` = 200 ([ADR 0004](adr/0004-hnsw-parameters.md)).

**Not yet measured:** the same index on the full 5.6M rows. Its build needs an estimated
9–10 GB of `maintenance_work_mem` (an extrapolation), more than this machine has.

## 2. Full-text keyword search: GIN

52 queries, top 100 by `ts_rank_cd`, on the same 500k-chunk sample. The GIN build took
10.8 s and the index is 64 MB.

| index | match | p50 ms | p95 ms | median matching chunks | queries with no match |
|---|---|---|---|---|---|
| none (seq scan) | all (AND) | 104.9 | 126.3 | 2 | 17 |
| none (seq scan) | any (OR) | 682.4 | 1,346.5 | 250,556 | 0 |
| GIN | all (AND) | 3.7 | 26.7 | 2 | 17 |
| GIN | any (OR) | 725.7 | 1,463.0 | 250,556 | 0 |

**Findings:**

- **GIN makes AND queries 28× faster at p50.**
- **GIN cannot help OR queries.** An OR of common words matches half the corpus, and
  `ts_rank_cd` must score every match.
- **AND is too strict for natural-language queries.** A third of them match nothing.

## 3. Search quality: keyword vs semantic vs hybrid

**Setup:**
- 52 queries written the way a consumer would phrase them.
- Each query is labelled with a CFPB product family, issue and sub-issue. Grade 2 means a
  complaint matches all three; grade 1 means it matches the family and issue only. The
  labels are the consumers' own choices when filing.
- Window: 2019, which has 124,843 complaints with narratives. Every arm is exact.
- 100 candidates per arm.

| system | nDCG@10 | recall@10 | recall@100 | MRR@10 | queries with 0 results |
|---|---|---|---|---|---|
| keyword, AND | 0.149 | 0.104 | 0.036 | 0.363 | 21 |
| keyword, OR | 0.199 | 0.167 | 0.145 | 0.319 | 0 |
| semantic | **0.564** | **0.506** | **0.392** | 0.728 | 0 |
| **hybrid, AND (default)** | 0.563 | 0.496 | 0.376 | **0.801** | 0 |
| hybrid, OR | 0.442 | 0.392 | 0.309 | 0.598 | 0 |

Paired bootstrap on nDCG@10 (10,000 resamples):

| comparison | mean diff | 95% CI | W / L / T |
|---|---|---|---|
| hybrid AND vs semantic | −0.0005 | [−0.036, +0.035] | 14 / 16 / 22 |
| hybrid OR vs hybrid AND | −0.121 | [−0.175, −0.065] | 14 / 38 / 0 |
| keyword OR vs keyword AND | +0.049 | [−0.029, +0.126] | 29 / 18 / 5 |
| semantic vs keyword OR | +0.365 | [+0.296, +0.436] | 48 / 4 / 0 |

**RRF sensitivity:** across `rrf_k` ∈ {1, 10, 30, 60, 100, 200} and keyword weights 0.25–2,
hybrid with AND stays within 0.533–0.582 nDCG@10.

**Findings:**

- **Hybrid with AND ties semantic on nDCG and wins on MRR.** It puts the first relevant
  complaint higher (MRR 0.80 vs 0.73).
- **OR matching floods the keyword arm with noise, and RRF passes it through.**
- **The defaults (k = 60, equal weights) are within noise of the best cell.** The best cell
  is 0.582, about 0.02 higher, and it was tuned on the same 52 queries, so I did not adopt
  it.

**Caveats:**

- The query set is paraphrases scored against topic labels, which favours semantic search.
  It does not test exact terms such as names, statutes or "Zelle", where keyword search
  earns its place.
- Labels are noisy, so absolute scores are a lower bound. The comparison is fair because
  every system sees the same labels.

**Decisions:** hybrid is the default mode, `keyword_match = all` the default, and k = 60
with equal weights ([ADR 0002](adr/0002-hybrid-search-rrf.md)).

### Two performance bugs the evaluation found

The first evaluation run took 9.5 minutes per query. Both causes are fixed:

| problem | before | after | fix |
|---|---|---|---|
| A one-year filter chose a parallel seq scan of 5.6M chunks | 64 s | 1.6 s | `random_page_cost` 4 → 1.1 (SSD) |
| OR tsquery in a materialized CTE, estimated at 0.5% selectivity against a real ~50%; GIN plan with 11 GB of temp files | 471 s | 5.4 s | CTE marked `NOT MATERIALIZED`, so the tsquery folds to a constant the planner can estimate |

## 4. Analytics SQL library

[`sql/analytics/`](../sql/analytics/) holds five queries over the marts. Server-side
execution time comes from `EXPLAIN (ANALYZE, TIMING OFF)`, with 5 runs; the median
excludes the first.

| query | technique | rows | median ms |
|---|---|---|---|
| `01_rolling_30d_timely_rate` | densified series + `ROWS` window, ratio of sums | 4,380 | 441.3 |
| `02_zscore_anomalies` | trailing 28-day baseline that excludes the current day | 14 | 694.6 |
| `03_timely_response_ranking` | Wilson lower bound, `rank` / `ntile` | 200 | 24.2 |
| `04_company_cohorts` | first-year cohorts, retention with `first_value` | 136 | 173.3 |
| `05_yoy_trends` | `lag` YoY, YTD running sums | 288 | 41.6 |

Re-measured on 2026-11-18, after the timely-rate fix (see the CHANGELOG). Before the fix
the same queries took 576, 756, 10.5, 167.5 and 49 ms; queries 01 and 03 now also read the
`responded` column.

## 5. Pipeline throughput (from the full load)

| job | volume | time | source |
|---|---|---|---|
| Bulk CSV backfill | 19.17M rows | 32 min | `ops.load_runs` |
| Incremental API load | 151,509 rows | 3 min 07 s (rerun: 16 s, 0 writes) | `ops.load_runs` |
| Narrative archive | 3.85M narratives, 21 files | about 20 min | `ops.load_runs` |
| dbt full build | 104 nodes, 19.17M-row fact | 11 min 50 s | dbt log, phase 3 |
| Embedding (RTX 5050, fp16) | 5,490,151 chunks | 4 h 03 min (376 chunks/s) | ADR 0006 |
