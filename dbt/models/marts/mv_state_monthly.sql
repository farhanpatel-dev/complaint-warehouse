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
    round(avg(f.timely_flag::int)::numeric, 4) as timely_rate,
    count(*) filter (where f.is_older_american) as older_american,
    count(*) filter (where f.is_servicemember) as servicemember
from {{ ref('fact_complaint') }} as f
inner join {{ ref('dim_geo') }} as g
    on f.geo_key = g.geo_key
group by 1, 2, 3, 4
