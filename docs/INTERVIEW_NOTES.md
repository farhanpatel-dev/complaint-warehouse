# Interview notes

My crib sheet for talking about this project: the pitch, the decisions I would defend, the
problems I hit, and the numbers worth remembering. Every number here is measured. The
sources are in [BENCHMARKS.md](BENCHMARKS.md) and the ADRs.

## The 60-second version

> I built a warehouse over the CFPB's public complaint database: 19 million complaints and
> 3.85 million consumer narratives, in one Postgres instance with pgvector. A Python job
> loads three official sources idempotently. dbt builds a star schema with SCD Type 2
> dimensions and a monthly partitioned fact. A resumable GPU job embeds 5.6 million
> narrative chunks. Search is hybrid, keyword plus semantic fused with Reciprocal Rank
> Fusion, inside a single SQL function behind a FastAPI endpoint.
>
> Then I measured it. I compared HNSW and IVFFlat on a 500k-vector sample against exact
> ground truth, and evaluated search quality on 52 labelled queries with bootstrap
> confidence intervals. The measurements changed the design: I picked HNSW on half-precision
> vectors, kept AND matching instead of OR, and fixed two query-planner problems that had
> made some searches take minutes.

## Architecture walk-through (whiteboard order)

1. **Sources.** The CFPB search API (daily increments), the bulk CSV (backfill) and the FOIA
   narrative archive. The CFPB stopped publishing narratives in August 2026, so I found the
   archive (ADR 0005).
2. **raw.** Hash-guarded upserts. A rerun writes zero rows, so `last_changed_at` is a
   trustworthy change signal.
3. **dbt.** Staging, then snapshots, then the star schema, then the marts. Everything
   downstream of raw is incremental on that change signal.
4. **search.** The chunks with their `tsvector` and vector columns, plus a facets table for
   filters.
5. **Embedding job.** Fills the vectors, checkpointed.
6. **The function and the API.**
7. **The bench harness.** It measures all of it, through the same function the API calls.

## Decisions I would defend

**Why Postgres and not Pinecone, Qdrant or Elasticsearch?** (ADR 0001)
- Filters are joins against the warehouse, not metadata I have to sync.
- Vectors commit in the same transaction as their text. My stale-text guard depends on
  that.
- 5.6M vectors fits one instance comfortably as `halfvec`.
- I'd revisit with sustained query load beyond what read replicas handle, or a graph that
  doesn't fit in one machine's RAM.

**Why RRF instead of a weighted score sum?** (ADR 0002)
- `ts_rank_cd` is unbounded and depends on document length. Cosine similarity bunches
  between 0.6 and 0.9. Combining them needs per-query normalisation, which is fragile.
- RRF only uses ranks.
- I measured the sensitivity: across k from 1 to 200 and keyword weights from 0.25 to 2,
  nDCG@10 stays within 0.533–0.582. The best cell was only about 0.02 better, and it was
  tuned on the same queries, so I kept the defaults.

**Why do it in SQL rather than in the application?**
- One round trip.
- Filters apply inside each arm before the LIMIT. Doing "top 100, then filter" can leave
  you with 3 results.
- The benchmark measures exactly the code users hit.

**Why HNSW on `halfvec` with m = 32?** (ADR 0004)
- At about 0.9 recall@10, HNSW takes 9.8 ms; IVFFlat takes 34–66 ms.
- `halfvec` costs at most 0.013 recall, and the index is half the size: 858 MB vs 1.6 GB.
- I chose on recall@100, not recall@10, because hybrid search consumes 100 candidates per
  arm.

**Why SCD2, and why backdate the first version?** (ADR 0003)
- The CFPB renamed its product taxonomy in 2017 and 2023, and my company curation changes.
- A snapshot stamps a company's first version with 2026, but its complaints start in 2011.
  Without backdating to 1900, every historical fact misses the join.
- The fact joins "as was"; the marts report "as is".

**Why 64-bit hash surrogate keys?**
- They are stable across rebuilds, so incremental facts never point at renumbered keys.
- 8 bytes, instead of a 33-byte text MD5, on a 19M-row fact.
- The collision probability is about 1e-11 at my dimension sizes.

**Why chunk the narratives?**
- One vector for a 2,000-word complaint averages away the detail a query is looking for.
- The model has a 512-token limit.
- 200 words is about 260–300 tokens. The 40-word overlap keeps a sentence that straddles a
  boundary whole in one chunk.

**Why a custom dbt materialization?**
- dbt-postgres creates tables with `CREATE TABLE AS`, which cannot declare partitions.
- My materialization stages rows in a temp table, creates the parent and any missing
  monthly partitions, then does delete+insert.

## Problems I found and fixed (the good stories)

| Problem | Symptom | Root cause | Fix | Result |
|---|---|---|---|---|
| Filtered search ignored the filter | one month + one product took 42 s | `no_filter OR id IN (subquery)`: Postgres converts `IN` into a semi-join *before* folding constants, and never under an `OR` | Split into filtered and unfiltered branches under `UNION ALL` with constant guards; the filter became a real join | 1.05 s |
| OR keyword queries ran away | 471 s, 11 GB of temp files | The tsquery sat in a materialized CTE, so the planner assumed 0.5% of chunks match; an OR query matches about 50% | `NOT MATERIALIZED`: after inlining, the tsquery folds to a constant and is estimated from statistics | 5.4 s |
| One-year filter chose a seq scan | 64 s | Default `random_page_cost = 4` models spinning disks | 1.1 for SSD | 1.6 s |
| Semantic arm returned 40 rows, not 100 | silent recall loss | HNSW returns at most `ef_search` rows (default 40) | `ef_search` 200 on the session; not on the function, because a `SET` clause stops inlining | – |
| IVFFlat silently became a seq scan | at `probes = 50` | The planner costed the index above a scan | The harness checks `EXPLAIN` and forces and marks the point | honest results |
| Marts vanished | `relation does not exist` | dbt rebuilds a dimension table with `DROP ... CASCADE`, which takes the materialized views with it | `--select dim_x+`; documented in the runbook | – |
| Changed mart SQL not applied | log said `REFRESH MATERIALIZED VIEW` | dbt-postgres compares MV config, not SQL | `--full-refresh` for changed MVs | – |
| Timely rates inflated | 651k in-progress complaints counted as timely | The CFPB publishes `Timely = Yes` before the company answers | A `responded` denominator in every mart | correct rates; league table re-ranked |
| Narratives disappeared from the live sources | API rejects the field; CSV has 15 columns | The CFPB stopped publishing narratives on 2026-08-14 | Load the FOIA archive (21 files) into its own table | 3.85M narratives |

How I found the planner problems: `EXPLAIN (ANALYZE, BUFFERS)` and comparing estimated row
counts with actual ones. In each case the estimate was off by orders of magnitude, or the
plan contained a node that shouldn't have been there.

## Evaluation: how I would explain it

- **ANN recall vs search quality.** Recall measures how much the index loses against exact
  search. Quality measures whether results are relevant. I measured them separately.
- **Ground truth for quality** is the consumer's own issue and sub-issue label, not my
  judgement. That makes it noisy but unbiased across systems, and the absolute scores are a
  lower bound.
- **Paired bootstrap** over queries. On 52 queries, hybrid and semantic tie
  (−0.0005, CI ±0.035). Hybrid wins on MRR, 0.80 vs 0.73. OR matching is significantly
  worse (−0.12, the CI excludes 0).
- **The weakness I state upfront:** the query set is paraphrases scored against topic
  labels, which favours semantic search. Exact-term queries (names, statutes) are where
  keyword search earns its place, and I haven't built that set yet.

## What I would do differently or next

- **Build the full-corpus HNSW index on a bigger machine.** On this laptop it needs an
  estimated 9–10 GB of build memory against 7.4 GB in Docker. I chose the parameters on a
  sample and was explicit that recall at 5.6M must be re-measured.
- **Write the exact-term query set** to quantify what the keyword arm adds.
- **Try `hnsw.iterative_scan`** (pgvector 0.8) for broad filters, and a cross-encoder
  reranker over the fused top 50.
- **Check the data semantics earlier.** I found the in-progress timely issue while writing
  the data dictionary. A profiling pass over each flag and its relationship to status would
  have caught it in phase 3.

## Numbers to remember

| | |
|---|---|
| Complaints / narratives / chunks | 19.17M / 3.85M / 5.6M |
| Fact partitions | 180 monthly |
| DB size | 52 GB (32 GB of it is chunks and vectors) |
| Bulk backfill | 19.17M rows in 32 min |
| dbt full build | about 12 min |
| Embedding | 4 h 03 min at 376 chunks/s, RTX 5050 laptop GPU |
| Exact vector scan, full corpus | 86–119 s |
| HNSW halfvec m32, ef 200 (500k sample) | recall@100 0.904 at 17.5 ms; 858 MB; 381 s build |
| nDCG@10: keyword AND / semantic / hybrid | 0.149 / 0.564 / 0.563 |
| MRR: hybrid vs semantic | 0.80 vs 0.73 |
| Planner fixes | 42 s → 1.05 s; 471 s → 5.4 s; 64 s → 1.6 s |
| Tests | 68 unit, 16 integration/e2e, 91 dbt tests |
