{#
    search.complaint_facets, grain: one row per complaint that has a narrative.

    Search filters (date, product family, company, state) and result display need a lookup
    by complaint_id. The fact table is partitioned by date_received, so a lookup by
    complaint_id alone would probe one index per monthly partition (180 of them). This small
    unpartitioned table, keyed on complaint_id, does the same job with a single index probe,
    and keeps the search schema independent of how the warehouse is physically laid out.

    product_family comes from the current taxonomy, so a filter on "Credit reporting"
    matches complaints filed under the pre-2017 and post-2023 product names alike.
#}

{{ config(
    materialized='table',
    post_hook=[
        "create unique index if not exists complaint_facets_pk on {{ this }} (complaint_id)",
        "create index if not exists complaint_facets_date_idx on {{ this }} (date_received)",
        "create index if not exists complaint_facets_family_idx on {{ this }} "
        ~ "(product_family, date_received)",
        "create index if not exists complaint_facets_company_idx on {{ this }} (company_name)",
        "analyze {{ this }}",
    ],
) }}

select
    c.complaint_id,
    c.date_received,
    c.company_name,
    c.product,
    c.sub_product,
    p.product_family,
    c.issue,
    c.state_code,
    c.response_type,
    c.timely_flag
from {{ ref('stg_cfpb__complaints') }} as c
inner join {{ ref('int_product_attributes') }} as p
    on
        c.product = p.product
        and c.sub_product = p.sub_product
where exists (
    select 1 from {{ ref('stg_cfpb__narratives') }} as n
    where n.complaint_id = c.complaint_id
)
