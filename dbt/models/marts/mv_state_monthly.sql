{#
    timely_rate counts only answered complaints. The CFPB publishes "Timely response? = Yes"
    on complaints still "In progress", which would inflate the most recent months.
#}
{{ config(
    indexes=[
        {'columns': ['month', 'state_code'], 'unique': True},
    ]
) }}

select
    date_trunc('month', f.date_received)::date as month,
    g.state_code,
    g.state_name,
    g.census_region,
    count(*) as complaints,
    count(*) filter (where f.response_type <> 'In progress') as responded,
    round(
        avg(f.timely_flag::int) filter (where f.response_type <> 'In progress')::numeric, 4
    ) as timely_rate,
    count(*) filter (where f.is_older_american) as older_american,
    count(*) filter (where f.is_servicemember) as servicemember
from {{ ref('fact_complaint') }} as f
inner join {{ ref('dim_geo') }} as g
    on f.geo_key = g.geo_key
group by 1, 2, 3, 4
