{#
    search.hybrid_search: full-text + semantic search fused with Reciprocal Rank Fusion.

    Created by the on-run-end hook (dbt_project.yml), so the function is versioned with the
    tables it reads. It is skipped until both tables exist, because a LANGUAGE sql body is
    validated at CREATE time.

    Each arm ranks chunks, keeps its best `candidate_count`, collapses them to complaints
    (a complaint ranks by its best chunk), and numbers them 1..n. RRF then scores each
    complaint as

        score = keyword_weight / (rrf_k + keyword_rank) + semantic_weight / (rrf_k + semantic_rank)

    where a complaint missing from an arm contributes 0 for that arm. RRF uses ranks only,
    so it needs no calibration between ts_rank_cd (unbounded, length-dependent) and cosine
    similarity (bounded, model-dependent), and a single arm cannot dominate through a
    score outlier. rrf_k = 60 is the constant from Cormack, Clarke & Buettcher (2009); larger
    values flatten the difference between top ranks.

    Setting a weight to 0 turns an arm off: its CTE has a constant-false filter that the
    planner turns into a one-time filter, so the arm is never executed. The evaluation in
    phase 6 uses this to compare keyword-only, semantic-only and hybrid on the same code path.

    The function is LANGUAGE sql, STABLE, with no SET clause, so Postgres can inline it into
    the calling query. NULL filters then fold away as constants, and the semantic arm keeps
    the `ORDER BY embedding <=> q LIMIT n` shape an HNSW or IVFFlat index can serve.
#}

{% macro create_hybrid_search() %}
    {#- Ask Postgres directly: dbt's relation cache is not populated in run-operation. -#}
    {%- if execute -%}
        {%- set ready = run_query(
            "select to_regclass('search.complaint_text') is not null"
            ~ " and to_regclass('search.complaint_facets') is not null"
        ).columns[0].values()[0] -%}
    {%- else -%}
        {%- set ready = false -%}
    {%- endif -%}
    {%- if ready -%}
    {#- After inlining the parameters are constants, so this folds to true or false at plan
        time and an unfiltered search carries no filter at all. -#}
    {%- set no_filter -%}
        (hybrid_search.date_from is null and hybrid_search.date_to is null
        and hybrid_search.product_family is null and hybrid_search.company_name is null
        and hybrid_search.state_code is null)
    {%- endset -%}
-- Adding a parameter creates a new overload instead of replacing the function, and two
-- overloads make named-argument calls ambiguous, so drop the signature before keyword_match.
drop function if exists search.hybrid_search(
    text, vector, integer, integer, double precision, double precision, integer,
    date, date, text, text, text
);

create or replace function search.hybrid_search(
    query_text text,
    query_embedding vector({{ var('embedding_dim') | int }}),
    match_count integer default 10,
    candidate_count integer default 100,
    keyword_weight double precision default 1.0,
    semantic_weight double precision default 1.0,
    rrf_k integer default 60,
    date_from date default null,
    date_to date default null,
    product_family text default null,
    company_name text default null,
    state_code text default null,
    keyword_match text default 'all'
)
returns table (
    complaint_id bigint,
    score double precision,
    keyword_rank integer,
    semantic_rank integer,
    keyword_score real,
    cosine_similarity double precision,
    best_chunk_no integer
)
language sql
stable
parallel safe
as $fn$
with

-- keyword_match 'all' requires every term (websearch_to_tsquery's AND); 'any' ORs the same
-- stemmed lexemes, so a long natural-language query still finds chunks that share only
-- some of its words. The cast back to tsquery keeps the lexemes as they are (no re-stemming).
--
-- NOT MATERIALIZED matters: the two-argument websearch_to_tsquery is immutable, so once the
-- function is inlined the planner folds this to a constant tsquery and estimates how many
-- chunks match from the tsv statistics. Materialized, it is opaque and the planner assumes
-- 0.5% of chunks match. That suits AND queries, but an OR query matching half the corpus
-- then fetched millions of rows through GIN before applying the filter (471 s, 11 GB of
-- temp files, measured in phase 6).
query as not materialized (
    select
        case
            when hybrid_search.keyword_match = 'any'
                then replace(websearch_to_tsquery('english', query_text)::text, ' & ', ' | ')::tsquery
            else websearch_to_tsquery('english', query_text)
        end as tsq
),

-- Filters are applied inside each arm, before the LIMIT, so a filtered search still returns
-- up to candidate_count matching candidates per arm. NOT MATERIALIZED lets each arm plan the
-- facet lookup with its indexes instead of scanning a materialized CTE.
allowed as not materialized (
    select f.complaint_id
    from search.complaint_facets as f
    where
        (hybrid_search.date_from is null or f.date_received >= hybrid_search.date_from)
        and (hybrid_search.date_to is null or f.date_received <= hybrid_search.date_to)
        and (hybrid_search.product_family is null or f.product_family = hybrid_search.product_family)
        and (hybrid_search.company_name is null or f.company_name = hybrid_search.company_name)
        and (hybrid_search.state_code is null or f.state_code = hybrid_search.state_code)
),

-- Each arm has an unfiltered and a filtered branch; a constant guard means exactly one runs.
-- Writing the filter as `no_filter OR complaint_id IN (...)` would be shorter, but Postgres
-- turns IN (subquery) into a semi-join before it folds constants, and an IN under an OR is
-- never converted, so a selective filter still meant a sequential scan of every chunk.
-- As a real join, the planner can start from the few allowed complaints.
keyword_chunks as (
    (
        select
            t.complaint_id,
            t.chunk_no,
            ts_rank_cd(t.tsv, query.tsq) as rank_score
        from search.complaint_text as t
        cross join query
        where
            keyword_weight > 0
            and {{ no_filter }}
            and t.tsv @@ query.tsq
        order by rank_score desc, t.complaint_id, t.chunk_no
        limit candidate_count
    )
    union all
    (
        select
            t.complaint_id,
            t.chunk_no,
            ts_rank_cd(t.tsv, query.tsq) as rank_score
        from allowed as a
        inner join search.complaint_text as t
            on a.complaint_id = t.complaint_id
        cross join query
        where
            keyword_weight > 0
            and not {{ no_filter }}
            and t.tsv @@ query.tsq
        order by rank_score desc, t.complaint_id, t.chunk_no
        limit candidate_count
    )
),

keyword as (
    select
        complaint_id,
        max(rank_score) as keyword_score,
        (array_agg(chunk_no order by rank_score desc, chunk_no))[1] as best_chunk_no,
        row_number() over (order by max(rank_score) desc, complaint_id)::integer as rnk
    from keyword_chunks
    group by complaint_id
),

semantic_chunks as (
    (
        -- Kept in the `ORDER BY embedding <=> q LIMIT n` shape an HNSW/IVFFlat index serves.
        select
            t.complaint_id,
            t.chunk_no,
            t.embedding <=> query_embedding as distance
        from search.complaint_text as t
        where
            semantic_weight > 0
            and {{ no_filter }}
            and t.embedding is not null
        order by t.embedding <=> query_embedding
        limit candidate_count
    )
    union all
    (
        select
            t.complaint_id,
            t.chunk_no,
            t.embedding <=> query_embedding as distance
        from allowed as a
        inner join search.complaint_text as t
            on a.complaint_id = t.complaint_id
        where
            semantic_weight > 0
            and not {{ no_filter }}
            and t.embedding is not null
        order by t.embedding <=> query_embedding
        limit candidate_count
    )
),

semantic as (
    select
        complaint_id,
        1 - min(distance) as cosine_similarity,
        (array_agg(chunk_no order by distance, chunk_no))[1] as best_chunk_no,
        row_number() over (order by min(distance), complaint_id)::integer as rnk
    from semantic_chunks
    group by complaint_id
),

fused as (
    select
        coalesce(k.complaint_id, s.complaint_id) as complaint_id,
        coalesce(keyword_weight / (rrf_k + k.rnk), 0)
        + coalesce(semantic_weight / (rrf_k + s.rnk), 0) as score,
        k.rnk as keyword_rank,
        s.rnk as semantic_rank,
        k.keyword_score,
        s.cosine_similarity,
        coalesce(s.best_chunk_no, k.best_chunk_no) as best_chunk_no
    from keyword as k
    full outer join semantic as s
        on k.complaint_id = s.complaint_id
)

select
    complaint_id,
    score,
    keyword_rank,
    semantic_rank,
    keyword_score,
    cosine_similarity,
    best_chunk_no
from fused
order by score desc, complaint_id
limit match_count
$fn$;

comment on function search.hybrid_search is
    'Keyword (ts_rank_cd) + semantic (pgvector cosine) search fused with Reciprocal Rank Fusion. Defined in dbt/macros/search/hybrid_search.sql.';
    {%- else -%}
select 1 -- search tables not built yet; hybrid_search is created on a later run
    {%- endif -%}
{% endmacro %}

{#- Reinstall the search function without rebuilding any model:
    dbt run-operation install_search_functions -#}
{% macro install_search_functions() %}
    {% set ddl = create_hybrid_search() %}
    {% if 'create or replace function' not in ddl %}
        {% do exceptions.raise_compiler_error(
            "search.complaint_text and search.complaint_facets must exist; run dbt build first"
        ) %}
    {% endif %}
    {% do run_query(ddl) %}
    {% do log("search.hybrid_search installed", info=True) %}
{% endmacro %}
