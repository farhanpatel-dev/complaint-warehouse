# Vector index study

501,685 chunks from 345,351 complaints (9.0% hash sample of `search.complaint_text`, 2378 MB). 200 queries (52 written evaluation queries, 148 CFPB issue/sub-issue names), embedded with `BAAI/bge-base-en-v1.5`. Ground truth: exact top-100 by cosine distance, computed client-side in 65.8 s.

Measured 2026-11-17T00:02:38+00:00 on PostgreSQL 16.10 (Debian 16.10-1.pgdg12+1), pgvector 0.8.0; shared_buffers 512MB, work_mem 64MB, max_parallel_workers_per_gather 2, random_page_cost 4 (default), jit on. Client container: 16 CPUs, 7770292 kB RAM.

## Build

| index | method | type | params | build s | size | notices |
|---|---|---|---|---|---|---|
| hnsw_vec_m16_efc64 | hnsw | vector | m=16, ef_construction=64 | 284.9 | 1637 MB | - |
| hnsw_vec_m32_efc128 | hnsw | vector | m=32, ef_construction=128 | 487.6 | 1616 MB | - |
| ivfflat_vec_l501 | ivfflat | vector | lists=501 | 158.5 | 1963 MB | - |
| hnsw_half_m16_efc64 | hnsw | halfvec | m=16, ef_construction=64 | 157.0 | 836 MB | - |
| hnsw_half_m32_efc128 | hnsw | halfvec | m=32, ef_construction=128 | 380.7 | 858 MB | - |
| ivfflat_half_l501 | ivfflat | halfvec | lists=501 | 87.8 | 786 MB | - |

## Recall@10 and latency (LIMIT 10, warm cache)

| index | setting | recall@10 | p50 ms | p95 ms | queries |
|---|---|---|---|---|---|
| exact | - | 1.0 | 1,080.2 | 1,152.0 | 20 |
| hnsw_vec_m16_efc64 | ef_search=10 | 0.49 | 1.3 | 2.4 | 200 |
| hnsw_vec_m16_efc64 | ef_search=20 | 0.605 | 2.5 | 4.0 | 200 |
| hnsw_vec_m16_efc64 | ef_search=40 | 0.7135 | 3.8 | 6.1 | 200 |
| hnsw_vec_m16_efc64 | ef_search=100 | 0.8305 | 7.2 | 12.2 | 200 |
| hnsw_vec_m16_efc64 | ef_search=200 | 0.882 | 12.5 | 17.2 | 200 |
| hnsw_vec_m16_efc64 | ef_search=400 | 0.921 | 21.4 | 31.2 | 200 |
| hnsw_vec_m32_efc128 | ef_search=10 | 0.6475 | 3.4 | 6.1 | 200 |
| hnsw_vec_m32_efc128 | ef_search=20 | 0.7355 | 4.2 | 6.3 | 200 |
| hnsw_vec_m32_efc128 | ef_search=40 | 0.8365 | 6.2 | 9.9 | 200 |
| hnsw_vec_m32_efc128 | ef_search=100 | 0.912 | 12.3 | 19.0 | 200 |
| hnsw_vec_m32_efc128 | ef_search=200 | 0.948 | 20.6 | 31.8 | 200 |
| hnsw_vec_m32_efc128 | ef_search=400 | 0.968 | 37.2 | 55.2 | 200 |
| ivfflat_vec_l501 | probes=1 | 0.303 | 2.4 | 7.2 | 200 |
| ivfflat_vec_l501 | probes=2 | 0.4365 | 3.3 | 12.0 | 200 |
| ivfflat_vec_l501 | probes=5 | 0.598 | 6.5 | 20.4 | 200 |
| ivfflat_vec_l501 | probes=10 | 0.7165 | 13.0 | 33.6 | 200 |
| ivfflat_vec_l501 | probes=20 | 0.812 | 23.8 | 67.2 | 200 |
| ivfflat_vec_l501 | probes=50 (forced) | 0.9125 | 66.1 | 144.2 | 200 |
| hnsw_half_m16_efc64 | ef_search=10 | 0.4995 | 1.2 | 1.9 | 200 |
| hnsw_half_m16_efc64 | ef_search=20 | 0.602 | 1.5 | 2.6 | 200 |
| hnsw_half_m16_efc64 | ef_search=40 | 0.707 | 3.0 | 5.3 | 200 |
| hnsw_half_m16_efc64 | ef_search=100 | 0.8205 | 5.2 | 7.8 | 200 |
| hnsw_half_m16_efc64 | ef_search=200 | 0.871 | 8.9 | 13.9 | 200 |
| hnsw_half_m16_efc64 | ef_search=400 | 0.912 | 15.4 | 24.1 | 200 |
| hnsw_half_m32_efc128 | ef_search=10 | 0.618 | 2.4 | 4.3 | 200 |
| hnsw_half_m32_efc128 | ef_search=20 | 0.7315 | 3.5 | 6.6 | 200 |
| hnsw_half_m32_efc128 | ef_search=40 | 0.8235 | 5.0 | 8.4 | 200 |
| hnsw_half_m32_efc128 | ef_search=100 | 0.906 | 9.8 | 15.4 | 200 |
| hnsw_half_m32_efc128 | ef_search=200 | 0.94 | 15.3 | 22.5 | 200 |
| hnsw_half_m32_efc128 | ef_search=400 | 0.9605 | 27.8 | 39.2 | 200 |
| ivfflat_half_l501 | probes=1 | 0.304 | 1.3 | 3.9 | 200 |
| ivfflat_half_l501 | probes=2 | 0.4145 | 2.2 | 5.9 | 200 |
| ivfflat_half_l501 | probes=5 | 0.5655 | 4.2 | 9.9 | 200 |
| ivfflat_half_l501 | probes=10 | 0.698 | 7.5 | 16.5 | 200 |
| ivfflat_half_l501 | probes=20 | 0.7955 | 13.6 | 28.3 | 200 |
| ivfflat_half_l501 | probes=50 | 0.894 | 33.6 | 72.0 | 200 |

## Recall@100 and latency (LIMIT 100, warm cache)

| index | setting | recall@100 | p50 ms | p95 ms | queries |
|---|---|---|---|---|---|
| exact | - | 1.0 | 1,098.9 | 1,184.5 | 20 |
| hnsw_vec_m16_efc64 | ef_search=100 | 0.7364 | 8.9 | 14.2 | 200 |
| hnsw_vec_m16_efc64 | ef_search=200 | 0.8178 | 14.3 | 19.6 | 200 |
| hnsw_vec_m16_efc64 | ef_search=400 | 0.8825 | 23.5 | 33.3 | 200 |
| hnsw_vec_m32_efc128 | ef_search=100 | 0.8473 | 13.4 | 19.4 | 200 |
| hnsw_vec_m32_efc128 | ef_search=200 | 0.9096 | 22.3 | 31.5 | 200 |
| hnsw_vec_m32_efc128 | ef_search=400 | 0.9503 | 39.4 | 56.3 | 200 |
| ivfflat_vec_l501 | probes=1 | 0.2425 | 3.1 | 8.1 | 200 |
| ivfflat_vec_l501 | probes=2 | 0.3575 | 3.6 | 11.3 | 200 |
| ivfflat_vec_l501 | probes=5 | 0.5174 | 7.9 | 22.3 | 200 |
| ivfflat_vec_l501 | probes=10 | 0.6437 | 14.6 | 35.4 | 200 |
| ivfflat_vec_l501 | probes=20 | 0.7514 | 28.9 | 67.5 | 200 |
| ivfflat_vec_l501 | probes=50 (forced) | 0.8789 | 68.4 | 148.9 | 200 |
| hnsw_half_m16_efc64 | ef_search=100 | 0.7325 | 7.5 | 12.6 | 200 |
| hnsw_half_m16_efc64 | ef_search=200 | 0.8191 | 11.3 | 16.2 | 200 |
| hnsw_half_m16_efc64 | ef_search=400 | 0.8772 | 19.8 | 29.8 | 200 |
| hnsw_half_m32_efc128 | ef_search=100 | 0.8447 | 11.5 | 16.6 | 200 |
| hnsw_half_m32_efc128 | ef_search=200 | 0.904 | 17.5 | 26.0 | 200 |
| hnsw_half_m32_efc128 | ef_search=400 | 0.9442 | 29.0 | 40.4 | 200 |
| ivfflat_half_l501 | probes=1 | 0.2453 | 2.5 | 4.3 | 200 |
| ivfflat_half_l501 | probes=2 | 0.3525 | 3.0 | 6.8 | 200 |
| ivfflat_half_l501 | probes=5 | 0.5115 | 5.2 | 11.2 | 200 |
| ivfflat_half_l501 | probes=10 | 0.6434 | 8.9 | 19.3 | 200 |
| ivfflat_half_l501 | probes=20 | 0.7501 | 15.8 | 31.4 | 200 |
| ivfflat_half_l501 | probes=50 | 0.871 | 36.1 | 70.9 | 200 |

## Full-text keyword search (GIN on tsv)

52 evaluation queries, top 100 by `ts_rank_cd`. GIN build 10.8 s, 64 MB.

| index | match | p50 ms | p95 ms | median matching chunks | no match |
|---|---|---|---|---|---|
| none (seq scan) | all | 104.9 | 126.3 | 2 | 17 |
| none (seq scan) | any | 682.4 | 1,346.5 | 250,556 | 0 |
| GIN | all | 3.7 | 26.7 | 2 | 17 |
| GIN | any | 725.7 | 1,463.0 | 250,556 | 0 |

(forced): the planner chose a sequential scan at this setting; the index was
measured with `enable_seqscan = off`.

Query plans: `bench/results/explain/index_*.txt`, `gin_*.txt`.
