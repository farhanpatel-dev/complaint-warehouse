# Analytics SQL library

Each query run 5 times with `EXPLAIN (ANALYZE, TIMING OFF)`; server-side execution time without per-node timing overhead. Median excludes the first run. The saved plans include per-node timing.

Measured 2026-11-18T06:20:42+00:00 on PostgreSQL 16.10 (Debian 16.10-1.pgdg12+1), pgvector 0.8.0; shared_buffers 512MB, work_mem 64MB, max_parallel_workers_per_gather 2, random_page_cost 1.1, jit on. Client container: 16 CPUs, 7770292 kB RAM.

| query | rows | first run ms | median ms | planning ms |
|---|---|---|---|---|
| 01_rolling_30d_timely_rate | 4,380 | 680.6 | 441.3 | 0.9 |
| 02_zscore_anomalies | 14 | 861.0 | 694.6 | 0.5 |
| 03_timely_response_ranking | 200 | 78.9 | 24.2 | 0.4 |
| 04_company_cohorts | 136 | 204.0 | 173.3 | 0.2 |
| 05_yoy_trends | 288 | 44.5 | 41.6 | 0.4 |

### 01_rolling_30d_timely_rate

`sql/analytics/01_rolling_30d_timely_rate.sql`, first 5 rows:

| date_day | product_family | complaints | complaints_30d | timely_rate_30d |
|---|---|---|---|---|
| 2025-11-15 | Bank accounts | 150 | 6033 | 0.9939 |
| 2025-11-16 | Bank accounts | 106 | 5931 | 0.9938 |
| 2025-11-17 | Bank accounts | 230 | 6048 | 0.9940 |
| 2025-11-18 | Bank accounts | 285 | 6246 | 0.9944 |
| 2025-11-19 | Bank accounts | 275 | 6325 | 0.9943 |

### 02_zscore_anomalies

`sql/analytics/02_zscore_anomalies.sql`, first 5 rows:

| date_day | company_name | complaints | mean_28d | stddev_28d | z_score |
|---|---|---|---|---|---|
| 2026-09-21 | CREDIT ACCEPTANCE CORPORATION | 42 | 7.3 | 4.7 | 7.3 |
| 2026-10-20 | Servicer under contract with Federal Student Aid | 26 | 7.3 | 2.7 | 7.0 |
| 2026-03-27 | Servicer under contract with Federal Student Aid | 75 | 17.2 | 10.0 | 5.8 |
| 2026-08-11 | Early Warning Services, LLC | 46 | 15.7 | 5.3 | 5.7 |
| 2026-11-03 | CBC Companies, Inc. | 116 | 32.1 | 16.3 | 5.1 |

### 03_timely_response_ranking

`sql/analytics/03_timely_response_ranking.sql`, first 5 rows:

| company_name | industry | complaints | timely_rate | timely_rate_lower_95 | overall_rank | industry_rank | quartile |
|---|---|---|---|---|---|---|---|
| TRANSUNION INTERMEDIATE HOLDINGS, INC. | Credit reporting agency | 2304999 | 1.0000 | 1.0000 | 1 | 1 | 1 |
| EQUIFAX, INC. | Credit reporting agency | 2048694 | 1.0000 | 1.0000 | 2 | 2 | 1 |
| Experian Information Solutions Inc. | Credit reporting agency | 2036606 | 1.0000 | 1.0000 | 3 | 3 | 1 |
| JPMORGAN CHASE & CO. | Bank | 27543 | 1.0000 | 0.9999 | 4 | 1 | 1 |
| ENCORE CAPITAL GROUP INC. | Debt buyer or collector | 23936 | 1.0000 | 0.9998 | 5 | 1 | 1 |

### 04_company_cohorts

`sql/analytics/04_company_cohorts.sql`, first 5 rows:

| cohort_year | years_since | active_companies | retention | complaints |
|---|---|---|---|---|
| 2,011 | 0 | 73 | 1.000 | 2536 |
| 2,011 | 1 | 71 | 0.973 | 65182 |
| 2,011 | 2 | 72 | 0.986 | 75711 |
| 2,011 | 3 | 71 | 0.973 | 73868 |
| 2,011 | 4 | 71 | 0.973 | 75399 |

### 05_yoy_trends

`sql/analytics/05_yoy_trends.sql`, first 5 rows:

| month | product_family | complaints | complaints_prior_year | yoy_pct | ytd | ytd_prior_year | ytd_yoy_pct |
|---|---|---|---|---|---|---|---|
| 2024-12-01 | Bank accounts | 4798 | 3821 | 25.6 | 52821 | 49873 | 5.9 |
| 2025-01-01 | Bank accounts | 18367 | 4032 | 355.5 | 18367 | 4032 | 355.5 |
| 2025-02-01 | Bank accounts | 5452 | 3833 | 42.2 | 23819 | 7865 | 202.8 |
| 2025-03-01 | Bank accounts | 5070 | 4257 | 19.1 | 28889 | 12122 | 138.3 |
| 2025-04-01 | Bank accounts | 5229 | 4327 | 20.8 | 34118 | 16449 | 107.4 |

Query plans: `bench/results/explain/analytics_*.txt`.
