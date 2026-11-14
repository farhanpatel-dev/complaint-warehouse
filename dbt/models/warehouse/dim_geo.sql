{#
    Grain: (state_code, zip_code) exactly as the CFPB publishes them, masked ZIPs included.
    I keep the published ZIP rather than rolling everything up to ZIP3, so analyses can
    choose their own level of detail; zip3 and the census hierarchy are attributes.
#}

with locations as (
    select distinct
        state_code,
        zip_code,
        zip3,
        is_zip_masked
    from {{ ref('stg_cfpb__complaints') }}
)

select
    {{ hash_key(['l.state_code', 'l.zip_code']) }} as geo_key,
    l.state_code,
    l.zip_code,
    l.zip3,
    l.is_zip_masked,
    coalesce(s.state_name, 'Unknown') as state_name,
    coalesce(s.census_region, 'Unknown') as census_region,
    coalesce(s.census_division, 'Unknown') as census_division,
    coalesce(s.jurisdiction_type, 'Unknown') as jurisdiction_type
from locations as l
left join {{ ref('us_states') }} as s
    on l.state_code = s.state_code

union all

-- The unknown member's natural key ('??') can never come out of staging, which uses '--'
-- for a missing state. If they matched, the fact join would fan out on those rows.
select
    {{ unknown_key() }} as geo_key,
    '??' as state_code,
    '?????' as zip_code,
    null as zip3,
    false as is_zip_masked,
    'Unknown member' as state_name,
    'Unknown member' as census_region,
    'Unknown member' as census_division,
    'Unknown member' as jurisdiction_type
