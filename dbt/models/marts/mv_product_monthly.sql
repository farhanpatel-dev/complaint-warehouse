{#
    Product family x product x month. product_family comes from the current taxonomy so
    trends line up across the CFPB's 2017 and 2023 product renames. timely_rate excludes
    in-progress complaints, which the CFPB marks timely before the company answers.
#}
{{ config(
    indexes=[
        {'columns': ['month', 'product_family', 'product'], 'unique': True},
        {'columns': ['product_family', 'month']},
    ]
) }}

with current_product as (
    select
        product_nk,
        product_family
    from {{ ref('dim_product') }}
    where is_current
)

select
    date_trunc('month', f.date_received)::date as month,
    cur.product_family,
    p.product,
    count(*) as complaints,
    count(*) filter (where f.response_type <> 'In progress') as responded,
    round(
        avg(f.timely_flag::int) filter (where f.response_type <> 'In progress')::numeric, 4
    ) as timely_rate,
    count(*) filter (where f.response_type = 'Closed with monetary relief') as monetary_relief,
    count(*) filter (where f.has_narrative) as with_narrative
from {{ ref('fact_complaint') }} as f
inner join {{ ref('dim_product') }} as p
    on f.product_key = p.product_key
inner join current_product as cur
    on p.product_nk = cur.product_nk
group by 1, 2, 3
