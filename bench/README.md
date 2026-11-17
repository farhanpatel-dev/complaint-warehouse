# Benchmarks

Everything here is produced by `python -m complaint_warehouse.bench` (`make bench ARGS=...`).
The JSON files in `results/` are the raw measurements. The Markdown next to them is generated
from the JSON (`make bench ARGS=report`), so no number in a report is typed in by hand.
Query plans are in `results/explain/`.

| Command | What it measures | Reads |
|---|---|---|
| `prepare` | Copies a 9% hash sample of complaints (all their chunks) into `bench.chunk_vectors`, embeds 200 queries, and computes the exact top-100 neighbours for each query. | `search.complaint_text` |
| `index` | HNSW and IVFFlat, `vector` and `halfvec`: build time, size, recall@10 / recall@100, p50 / p95 latency across `ef_search` and `probes`. Includes an exact-scan baseline. | `bench.*` |
| `gin` | Full-text top-100 with and without a GIN index, AND vs OR matching. | `bench.chunk_vectors` |
| `quality` | Keyword vs semantic vs hybrid on 52 labelled queries: nDCG@10, recall@10/100, MRR, latency, paired bootstrap CIs, RRF sensitivity. | full corpus, 2019 window |
| `analytics` | Server-side execution time of every query in `sql/analytics/`. | marts |

## Reproducing

```bash
make up
make bench ARGS=prepare      # about 10 min; embeds queries on CPU
make bench ARGS=index        # builds 7 indexes one at a time; resumable, skips finished ones
make bench ARGS=gin
make bench ARGS=quality
make bench ARGS=analytics
```

`index` saves after each index, so a run that dies keeps everything measured so far. Use
`--only NAME` to run one configuration, and `--force` to measure it again.

## Method notes

- **Latency is warm-cache:** each setting runs every query once untimed, then once timed.
  It is measured at the client over a local socket, so it includes about 0.2 ms of round
  trip.
- **The planner is checked:** a plain `EXPLAIN` runs before timing, and the run fails if the
  intended index is not used.
- **Recall is ANN recall:** the overlap with the exact top-k for the same query vector. It
  measures how much the approximation loses, not whether the results are relevant. That is
  the separate `quality` question.
- **Quality labels are the consumer's own:** the CFPB issue and sub-issue chosen when filing
  the complaint. See `queries/eval_queries.toml`. They are noisy but identical for every
  method, so the comparison is fair, and absolute scores are a lower bound.
