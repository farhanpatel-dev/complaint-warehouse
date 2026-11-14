{#
    Company x month scorecard. Reported "as is": every month uses the company's *current*
    parent group and industry, so a reclassification changes the whole history consistently.
    The fact keeps the "as was" keys for analyses that need the historical view.
#}
{{ config(
    indexes=[
        {'columns': ['month', 'company_name'], 'unique': True},
        {'columns': ['company_name']},
    ]
) }}

with current_company as (
    select
        company_name,
        parent_group,
        industry
    from {{ ref('dim_company') }}
    where is_current
)

select
    date_trunc('month', f.date_received)::date as month,
    c.company_name,
    cur.parent_group,
    cur.industry,
    count(*) as complaints,
    count(*) filter (where f.timely_flag) as timely_responses,
    round(avg(f.timely_flag::int)::numeric, 4) as timely_rate,
    count(*) filter (where f.response_type = 'Closed with monetary relief') as monetary_relief,
    count(*) filter (where f.response_type = 'Closed with non-monetary relief') as non_monetary_relief,
    count(*) filter (where f.response_type = 'In progress') as in_progress,
    count(*) filter (where f.has_narrative) as with_narrative
from {{ ref('fact_complaint') }} as f
inner join {{ ref('dim_company') }} as c
    on f.company_key = c.company_key
inner join current_company as cur
    on c.company_name = cur.company_name
group by 1, 2, 3, 4
