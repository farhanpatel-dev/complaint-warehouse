{#
    SCD Type 2 product dimension built from snap_product. Same windowing rules as dim_company:
    half-open [valid_from, valid_to), with the first version backdated to 1900-01-01.
#}

with snapshot as (
    select * from {{ ref('snap_product') }}
),

versions as (
    select
        product_nk,
        product,
        sub_product,
        product_family,
        is_current_name,
        case
            when row_number() over (partition by product_nk order by dbt_valid_from) = 1
                then '1900-01-01'::timestamp
            else dbt_valid_from
        end as valid_from,
        coalesce(dbt_valid_to, '9999-12-31'::timestamp) as valid_to,
        dbt_valid_to is null as is_current
    from snapshot
)

select
    {{ hash_key(['product_nk', 'valid_from']) }} as product_key,
    product_nk,
    product,
    sub_product,
    product_family,
    is_current_name,
    valid_from,
    valid_to,
    is_current
from versions

union all

select
    {{ unknown_key() }} as product_key,
    'Unknown member' as product_nk,
    'Unknown member' as product,
    'Unknown member' as sub_product,
    'Unknown member' as product_family,
    false as is_current_name,
    '1900-01-01'::timestamp as valid_from,
    '9999-12-31'::timestamp as valid_to,
    true as is_current
