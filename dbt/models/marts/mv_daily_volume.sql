{#
    Daily complaint counts per company and product family: the base for the rolling-rate
    and z-score anomaly queries in sql/analytics. Days with no complaints are absent; the
    analytics queries densify against dim_date where a gap-free series matters.
#}
{{ config(
    indexes=[
        {'columns': ['date_received', 'company_name', 'product_family'], 'unique': True},
        {'columns': ['company_name', 'date_received']},
    ]
) }}

select
    f.date_received,
    c.company_name,
    p.product_family,
    count(*) as complaints,
    count(*) filter (where f.timely_flag) as timely_responses
from {{ ref('fact_complaint') }} as f
inner join {{ ref('dim_company') }} as c
    on f.company_key = c.company_key
inner join {{ ref('dim_product') }} as p
    on f.product_key = p.product_key
group by 1, 2, 3
