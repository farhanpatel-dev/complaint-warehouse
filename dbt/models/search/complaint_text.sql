{#
    search.complaint_text, grain: one row per (complaint_id, chunk_no).

    Narratives are split into overlapping word windows (var chunk_words, step chunk_words -
    chunk_overlap_words). Long narratives would otherwise be truncated by the embedding model's
    token limit, and a single vector for a 2,000-word complaint averages away the specific
    detail a query is looking for. The overlap keeps a sentence that straddles a boundary
    intact in at least one chunk.

    Chunk count for n words, size s, step t:  1 + ceil(max(n - s, 0) / t)

    tsv is computed here so full-text and vector search see exactly the same text.
    `embedding` starts NULL and is filled by the embedding job (phase 4). When a narrative
    changes, delete+insert recreates its chunks with a NULL embedding, so stale vectors can't
    survive a text change.

    full_refresh=false: a careless `dbt build --full-refresh` must not drop the embeddings,
    which take hours of compute to rebuild.
#}

{{ config(
    materialized='incremental',
    unique_key='complaint_id',
    incremental_strategy='delete+insert',
    full_refresh=false,
    on_schema_change='fail',
    post_hook=[
        "create unique index if not exists complaint_text_pk on {{ this }} (complaint_id, chunk_no)",
        "create index if not exists complaint_text_tsv_gin on {{ this }} using gin (tsv)",
        "create index if not exists complaint_text_pending_idx on {{ this }} (complaint_id, chunk_no) where embedding is null",
        "analyze {{ this }}",
    ],
) }}

{% set size = var('chunk_words') | int %}
{% set step = (var('chunk_words') | int) - (var('chunk_overlap_words') | int) %}

with narratives as (
    select
        complaint_id,
        narrative_clean,
        last_changed_at
    from {{ ref('stg_cfpb__narratives') }}
    where
        narrative_clean <> ''
        {% if is_incremental() %}
            and last_changed_at > (
                select coalesce(max(existing.source_changed_at), '-infinity'::timestamptz)
                from {{ this }} as existing
            )
        {% endif %}
),

words as (
    select
        complaint_id,
        last_changed_at,
        regexp_split_to_array(narrative_clean, '\s+') as w
    from narratives
),

chunks as (
    select
        words.complaint_id,
        words.last_changed_at,
        gs.chunk_no,
        array_to_string(
            words.w[(gs.chunk_no * {{ step }}) + 1:(gs.chunk_no * {{ step }}) + {{ size }}], ' '
        ) as narrative
    from words
    cross join lateral generate_series(
        0,
        ceil(greatest(cardinality(words.w) - {{ size }}, 0)::numeric / {{ step }})::int
    ) as gs (chunk_no)
)

select
    complaint_id,
    chunk_no,
    narrative,
    to_tsvector('english', narrative) as tsv,
    null::vector({{ var('embedding_dim') | int }}) as embedding,
    null::text as embedding_model,
    null::timestamptz as embedded_at,
    last_changed_at as source_changed_at
from chunks
