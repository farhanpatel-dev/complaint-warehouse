# Search quality: keyword vs semantic vs hybrid

52 labelled queries over complaints received 2019-01-01 to 2019-12-31 (124,843 complaints with narratives), 100 candidate chunks per arm, model `BAAI/bge-base-en-v1.5`. Relevance from CFPB issue/sub-issue labels: grade 2 exact sub-issue, grade 1 same issue. Recall is capped recall (hits / min(k, relevant)) on grade-2 complaints.

Measured 2026-11-17T01:31:56+00:00 on PostgreSQL 16.10 (Debian 16.10-1.pgdg12+1), pgvector 0.8.0; shared_buffers 512MB, work_mem 64MB, max_parallel_workers_per_gather 2, random_page_cost 1.1, jit on. Client container: 16 CPUs, 7770292 kB RAM.

## Summary

| system | nDCG@10 | recall@10 | recall@100 | MRR@10 | zero results | p50 ms | p95 ms |
|---|---|---|---|---|---|---|---|
| keyword_all | 0.1493 | 0.1038 | 0.0363 | 0.3625 | 21 | 387.7 | 5,389.3 |
| keyword_any | 0.1985 | 0.1673 | 0.1452 | 0.319 | 0 | 1,127.5 | 60,838.5 |
| semantic | 0.5637 | 0.5058 | 0.3915 | 0.7278 | 0 | 1,220.9 | 8,016.3 |
| hybrid_all | 0.5632 | 0.4962 | 0.3758 | 0.8013 | 0 | 1,389.5 | 2,230.6 |
| hybrid_any | 0.4424 | 0.3923 | 0.3092 | 0.5981 | 0 | 2,073.6 | 43,477.6 |

Latency caveat: for each query the systems run in the order above, so the hybrid calls find the 2019 vectors already cached by the semantic call. Compare quality across systems; read latency within one system, or from the index study.

## Paired comparisons (nDCG@10, 10,000 bootstrap resamples of the queries)

| comparison | mean diff | 95% CI | wins | losses | ties |
|---|---|---|---|---|---|
| hybrid_any vs semantic | -0.1213 | [-0.170, -0.073] | 14 | 37 | 1 |
| hybrid_all vs semantic | -0.0005 | [-0.036, +0.035] | 14 | 16 | 22 |
| hybrid_any vs hybrid_all | -0.1208 | [-0.175, -0.065] | 14 | 38 | 0 |
| keyword_any vs keyword_all | 0.0492 | [-0.029, +0.126] | 29 | 18 | 5 |
| semantic vs keyword_any | 0.3652 | [+0.296, +0.436] | 48 | 4 | 0 |

## RRF sensitivity (nDCG@10; semantic weight 1)

keyword_match = `all`:

| rrf_k \ keyword weight | 0.25 | 0.5 | 1.0 | 2.0 |
|---|---|---|---|---|
| 1 | 0.5744 | 0.5677 | 0.5505 | 0.5446 |
| 10 | 0.5722 | 0.5759 | 0.5595 | 0.5328 |
| 30 | 0.5716 | 0.577 | 0.5609 | 0.5391 |
| 60 | 0.5758 | 0.5807 | 0.5632 | 0.5424 |
| 100 | 0.576 | 0.581 | 0.5668 | 0.5445 |
| 200 | 0.5789 | 0.5818 | 0.5669 | 0.5467 |

keyword_match = `any`:

| rrf_k \ keyword weight | 0.25 | 0.5 | 1.0 | 2.0 |
|---|---|---|---|---|
| 1 | 0.5484 | 0.4938 | 0.4129 | 0.2988 |
| 10 | 0.5621 | 0.562 | 0.4255 | 0.2286 |
| 30 | 0.5572 | 0.5538 | 0.436 | 0.2653 |
| 60 | 0.5431 | 0.5332 | 0.4424 | 0.3135 |
| 100 | 0.5429 | 0.5137 | 0.4447 | 0.3489 |
| 200 | 0.5187 | 0.5019 | 0.4438 | 0.366 |

## Per query (nDCG@10)

| query | relevant | keyword_all | keyword_any | semantic | hybrid_all | hybrid_any |
|---|---|---|---|---|---|---|
| bank-deposit-hold | 1,127 | 0.2201 | 0.1845 | 0.9368 | 0.8377 | 0.6949 |
| bank-unauthorized-withdrawal | 715 | 0 | 0 | 0.2646 | 0.2464 | 0.02208 |
| bank-debit-card-declined | 701 | 0 | 0.2201 | 0.3702 | 0.3702 | 0.4921 |
| bank-overdraft-fees | 387 | 0 | 0.4545 | 0.8533 | 0.8533 | 0.6164 |
| bank-closed-account | 359 | 0 | 0.1871 | 0.8401 | 0.5008 | 0.7265 |
| bank-closed-balance-not-returned | 263 | 0 | 0.02445 | 0.504 | 0.504 | 0.3506 |
| bank-signup-bonus | 439 | 0 | 0.4252 | 0.7827 | 0.7827 | 0.8553 |
| bank-fraud-account-opened | 129 | 0.07336 | 0 | 0.07336 | 0.07336 | 0.04629 |
| bank-wont-cash-check | 131 | 0.04928 | 0.2683 | 0.4951 | 0.2491 | 0.2075 |
| card-dispute-merchant | 1,921 | 0 | 0.3542 | 0.6123 | 0.6123 | 0.6417 |
| card-charges-not-mine | 935 | 0.638 | 0 | 0.06625 | 0.4974 | 0.1755 |
| card-annual-fee | 969 | 0 | 0.6067 | 0.6021 | 0.6021 | 0.615 |
| card-apr-increase | 137 | 0.2664 | 0.1601 | 0.4833 | 0.4051 | 0.4794 |
| card-closed-inactivity | 681 | 0.4537 | 0.02445 | 0.3152 | 0.5728 | 0.1682 |
| card-rewards-lost | 421 | 0 | 0.06625 | 0.8048 | 0.8048 | 0.2761 |
| card-intro-apr | 553 | 0 | 0.09997 | 0.4607 | 0.4607 | 0.3462 |
| card-identity-theft | 475 | 0.09479 | 0.04329 | 0 | 0.06362 | 0 |
| card-application-denied | 347 | 0.1755 | 0.05451 | 0.5685 | 0.518 | 0.213 |
| card-unsolicited | 110 | 0.2895 | 0.1331 | 0.4672 | 0.4904 | 0.4085 |
| card-credit-limit | 212 | 0.2201 | 0 | 0.7285 | 0.7683 | 0.4627 |
| report-not-my-accounts | 17,638 | 0.6208 | 0.3594 | 0.7799 | 0.9149 | 0.5463 |
| report-dispute-verified | 11,345 | 0.2201 | 0.4249 | 0.2428 | 0.3602 | 0.3592 |
| report-hard-inquiries | 4,363 | 1.0 | 0.5501 | 1.0 | 0.9052 | 0.89 |
| report-wrong-address | 1,950 | 0 | 0.2312 | 0.6237 | 0.5508 | 0.3999 |
| report-dispute-overdue | 1,611 | 0 | 0.1435 | 0.5855 | 0.3352 | 0.4095 |
| report-deleted-item-returns | 1,277 | 0.3347 | 0.184 | 0 | 0.301 | 0.07894 |
| report-bankruptcy-public-record | 1,261 | 0 | 0.4927 | 0.7952 | 0.7952 | 0.8922 |
| report-security-freeze | 1,251 | 0 | 0.35 | 0.6371 | 0.3636 | 0.5965 |
| report-free-annual | 511 | 0.65 | 0 | 1.0 | 0.9052 | 0.7818 |
| report-monitoring-cancel | 138 | 0.2201 | 0.1478 | 0.3174 | 0.4974 | 0.2906 |
| debt-not-mine | 5,193 | 0 | 0 | 0.702 | 0.702 | 0.4074 |
| debt-already-paid | 2,383 | 0.359 | 0 | 0.4104 | 0.6342 | 0.4249 |
| debt-repeated-calls | 1,490 | 0 | 0.2485 | 0.7472 | 0.7472 | 0.6416 |
| debt-stop-contact | 857 | 0 | 0.4269 | 0.7231 | 0.5927 | 0.335 |
| debt-threatened-arrest | 215 | 0 | 0 | 0.3166 | 0.3166 | 0.1389 |
| debt-called-employer | 199 | 0.2201 | 0 | 0.4763 | 0.5747 | 0.1319 |
| debt-fake-lawyer | 361 | 0 | 0.11 | 0.1514 | 0 | 0.2547 |
| debt-discharged-bankruptcy | 528 | 0.2201 | 0.03668 | 0.4305 | 0.3854 | 0.2749 |
| debt-no-validation | 2,555 | 0.1319 | 0.4419 | 0.4606 | 0.3319 | 0.6209 |
| debt-abusive-language | 314 | 0 | 0.02314 | 0.3947 | 0.3947 | 0.2952 |
| mortgage-misapplied-payments | 5,131 | 0.2201 | 0.2533 | 0.8572 | 0.8643 | 0.688 |
| mortgage-modification-foreclosure | 2,460 | 0 | 0.5054 | 0.9306 | 0.9306 | 0.4323 |
| mortgage-closing-delayed | 958 | 0 | 0.06625 | 0.9364 | 0.9364 | 0.9216 |
| mortgage-refinance-denied | 1,209 | 0.5075 | 0.542 | 0.6906 | 0.6332 | 0.7859 |
| student-pslf-wrong-info | 1,045 | 0.2201 | 0.4352 | 0.7511 | 0.8139 | 0.8528 |
| student-extra-payments | 984 | 0 | 0.5579 | 0.6021 | 0.6021 | 0.8611 |
| student-income-driven | 344 | 0.359 | 0.05605 | 0.3559 | 0.5204 | 0.105 |
| auto-deficiency-balance | 184 | 0 | 0.2719 | 0.3273 | 0.3273 | 0.2032 |
| auto-title-not-released | 164 | 0 | 0.06943 | 0.6987 | 0.6987 | 0.3967 |
| transfer-scam | 1,138 | 0 | 0 | 0.7264 | 0.7264 | 0.5474 |
| payday-unexpected-fees | 621 | 0 | 0 | 0.7191 | 0.7191 | 0.3569 |
| transfer-not-delivered | 275 | 0 | 0.08514 | 0.6911 | 0.6911 | 0.2832 |
