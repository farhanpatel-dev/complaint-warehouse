{#
    Current attributes per company: the input that snap_company versions.
    Curated groups come from the company_groups seed; everything else defaults to itself.
#}

with companies as (
    select distinct company_name
    from {{ ref('stg_cfpb__complaints') }}
)

select
    c.company_name,
    coalesce(g.parent_group, c.company_name) as parent_group,
    coalesce(g.industry, 'Unclassified') as industry
from companies as c
left join {{ ref('company_groups') }} as g
    on c.company_name = g.company_name
