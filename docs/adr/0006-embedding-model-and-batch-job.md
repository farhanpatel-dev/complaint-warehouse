# ADR 0006: Embedding model and the resumable batch job

- **Status:** Accepted
- **Date:** 2026-11-14
- **Deciders:** Farhan Patel

## Context

Semantic search needs a vector for each of the 5.6M narrative chunks in
`search.complaint_text`. The column is `vector(768)`. I had three constraints:

- **Open source and self-hosted.** No per-call API cost, no complaint text sent to a third
  party, and the same model must embed both the corpus and live queries.
- **It runs on the hardware I have**: a laptop with an RTX 5050 (8 GB) and 7.4 GB of RAM
  for Docker. The job also has to run on CPU, more slowly, for anyone without a GPU.
- **Long runs fail.** A multi-hour job will hit an OOM kill, a laptop sleep or a Ctrl+C at
  some point. Restarting from zero is not acceptable.

## Decision

### Model: `BAAI/bge-base-en-v1.5`

| Candidate | Dim | Max tokens | Why not / why |
|---|---|---|---|
| `all-mpnet-base-v2` | 768 | 384 | Solid general model, but trained mainly for sentence similarity rather than query → passage retrieval. |
| `intfloat/e5-base-v2` | 768 | 512 | Comparable quality; needs `query:` / `passage:` prefixes on both sides. |
| `nomic-embed-text-v1.5` | 768 | 8192 | The long context isn't needed with 200-word chunks, and it requires `trust_remote_code`. |
| **`bge-base-en-v1.5`** | 768 | 512 | Trained for retrieval, MIT licensed, no remote code. Only queries take an instruction prefix. |

A 200-word chunk is about 260–300 tokens, well inside the 512-token limit, so nothing is
truncated. Vectors are L2-normalised at encode time. Cosine distance (`<=>`) and inner
product then rank identically, and the HNSW/IVFFlat opclass choice in phase 6 does not
change results.

On CUDA the model runs in fp16. On normalised vectors the cosine difference from fp32 shows
up around the 4th decimal place, and fp16 roughly doubles throughput.

### Job design

- **Checkpoint in the same transaction as the vectors.** `ops.embedding_checkpoints`
  stores the last `(complaint_id, chunk_no)` written. Each batch's UPDATE and its
  checkpoint commit together, so a crash loses at most one uncommitted batch, and a rerun
  starts after the last committed key without rescanning. The keyset walk uses the
  `embedding IS NULL` partial index that dbt creates on the table.
- **Stale-text guard.** dbt rebuilds a narrative's chunks with delete+insert when the text
  changes. The UPDATE only applies a vector if `md5(narrative)` still matches the text that
  was embedded, so an old vector can never land on new text. Skipped chunks stay NULL and
  the next sweep picks them up.
- **One model per column.** Vectors from different models are not comparable. The job
  refuses to run if another model has embedded into the table until `embed reset` clears
  those vectors.
- **Overlapped writes.** A single writer thread with its own connection commits batch k
  while the GPU encodes batch k+1. One thread keeps checkpoints strictly in key order.
- **No transaction is held open during encoding.** The fetch commits before the GPU step,
  so a slow batch never holds locks or blocks vacuum.

## Measurements (RTX 5050 Laptop, fp16, CUDA 12.8 build of torch)

| Configuration | Throughput | Notes |
|---|---|---|
| Sequential fetch → encode → write | 273 chunks/s | encode 54 s, write 18 s, fetch 2 s per 20k chunks |
| Writes overlapped (batch 128, fetch 2048) | 358 chunks/s | main loop waited ~0 s on writes: GPU-bound |
| batch 64, fetch 2048 | 380 chunks/s | |
| batch 128, fetch 4096 | 361 chunks/s | |
| batch 256, fetch 8192 | 276 chunks/s | too few batches to overlap; more padding |

The 128/2048 and 64/2048 results are within run-to-run noise, so I kept 128/2048.
Overlapping writes gave about +30%. Past that, the GPU is the limit.

I also considered dropping the GIN and partial indexes during the backfill and rebuilding
them afterwards, a standard bulk-load trick. I decided against it: writes run at about
1,100 chunks/s, three times faster than encoding, and once they are overlapped they cost
nothing. Dropping indexes that dbt owns from a Python job would add a failure mode for no
gain.

## Consequences

- Full-corpus embedding is a one-off of a few hours on a laptop GPU. Incremental runs
  only embed new or re-chunked narratives.
- Every vector in `search.complaint_text` carries `embedding_model` and `embedded_at`.
  Switching models is an explicit `reset` followed by a re-embed, never a silent mix.
- Updating each row once leaves dead tuples behind (the vectors are TOASTed, about 3 KB
  each). Autovacuum reclaims the space over the run. The runbook covers checking bloat
  afterwards.
- The Docker image ships the CPU build of torch by default (CI, API). The GPU build is a
  compose override (`docker-compose.gpu.yml`, `make embed GPU=1`), since CUDA wheels
  make the image several GB larger.
