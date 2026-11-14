{#
    fact_complaint, grain: one row per complaint.

    Partitioned by month on date_received (see macros/materializations/partitioned_incremental).
    Most dashboard and analytics queries filter on a date range, so partition pruning lets them
    read only the months they need, and old months can be detached or archived without
    rewriting the table.

    SCD2 lookups are "as was": a complaint joins to the company and product version that was
    valid on its date_received.

    Incremental: a run only processes complaints whose raw row or narrative changed since the
    newest source_changed_at already in the fact.
#}

{{ config(
    materialized='partitioned_incremental',
    partition_column='date_received',
    unique_key='complaint_id',
    post_hook=[
        "create unique index if not exists fact_complaint_pk on {{ this }} (complaint_id, date_received)",
        "create index if not exists fact_complaint_company_idx on {{ this }} (company_key, date_received)",
        "create index if not exists fact_complaint_product_idx on {{ this }} (product_key, date_received)",
        "create index if not exists fact_complaint_geo_idx on {{ this }} (geo_key)",
        "create index if not exists fact_complaint_changed_idx on {{ this }} (source_changed_at)",
        "analyze {{ this }}",
    ],
) }}

{% set incremental = cw_is_incremental() %}

with
{% if incremental %}
    watermark as (
        select coalesce(max(source_changed_at), '-infinity'::timestamptz) as ts from {{ this }}
    ),

    -- sqlfluff lints each if-branch separately and misses the join below.
    changed as (  -- noqa: ST03
        select complaint_id from {{ ref('stg_cfpb__complaints') }}
        where last_changed_at > (select watermark.ts from watermark)
        union
        select complaint_id from {{ ref('stg_cfpb__narratives') }}
        where last_changed_at > (select watermark.ts from watermark)
    ),
{% endif %}

complaints as (
    select c.*
    from {{ ref('stg_cfpb__complaints') }} as c
    {% if incremental %}
        inner join changed on c.complaint_id = changed.complaint_id
    {% endif %}
),

narratives as (
    select
        complaint_id,
        last_changed_at
    from {{ ref('stg_cfpb__narratives') }}
)

select
    c.complaint_id,
    {{ date_key('c.date_received') }} as date_key,
    c.date_received,
    coalesce(co.company_key, {{ unknown_key() }}) as company_key,
    coalesce(p.product_key, {{ unknown_key() }}) as product_key,
    coalesce(g.geo_key, {{ unknown_key() }}) as geo_key,
    c.response_type,
    c.timely_flag,
    c.disputed_flag,
    c.submitted_via,
    c.days_to_company,
    c.is_older_american,
    c.is_servicemember,
    n.complaint_id is not null as has_narrative,
    greatest(c.last_changed_at, n.last_changed_at) as source_changed_at
from complaints as c
left join {{ ref('dim_company') }} as co
    on
        c.company_name = co.company_name
        and c.date_received >= co.valid_from
        and c.date_received < co.valid_to
left join {{ ref('dim_product') }} as p
    on
        c.product || ' | ' || c.sub_product = p.product_nk
        and c.date_received >= p.valid_from
        and c.date_received < p.valid_to
left join {{ ref('dim_geo') }} as g
    on
        c.state_code = g.state_code
        and c.zip_code = g.zip_code
left join narratives as n
    on c.complaint_id = n.complaint_id
