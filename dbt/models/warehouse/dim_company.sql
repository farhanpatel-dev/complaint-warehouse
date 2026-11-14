{#
    SCD Type 2 company dimension built from snap_company.

    Validity windows are half-open [valid_from, valid_to). A snapshot stamps a company's
    first version with the time dbt first saw it, but complaints about that company go back
    to 2011, so I backdate each company's first version to 1900-01-01. Without that, every
    historical fact would fall before its company's first version and miss the join.

    company_key hashes (company_name, valid_from), so it identifies a version and stays
    stable across rebuilds.
#}

with snapshot as (
    select * from {{ ref('snap_company') }}
),

versions as (
    select
        company_name,
        parent_group,
        industry,
        case
            when row_number() over (partition by company_name order by dbt_valid_from) = 1
                then '1900-01-01'::timestamp
            else dbt_valid_from
        end as valid_from,
        coalesce(dbt_valid_to, '9999-12-31'::timestamp) as valid_to,
        dbt_valid_to is null as is_current
    from snapshot
)

select
    {{ hash_key(['company_name', 'valid_from']) }} as company_key,
    company_name,
    parent_group,
    industry,
    valid_from,
    valid_to,
    is_current
from versions

union all

select
    {{ unknown_key() }} as company_key,
    'Unknown member' as company_name,
    'Unknown member' as parent_group,
    'Unknown member' as industry,
    '1900-01-01'::timestamp as valid_from,
    '9999-12-31'::timestamp as valid_to,
    true as is_current
