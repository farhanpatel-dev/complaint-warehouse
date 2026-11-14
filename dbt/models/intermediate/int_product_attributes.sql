{#
    Current attributes per (product, sub_product): the input that snap_product versions.
    product_nk is a single-column natural key, because dbt snapshots need one unique_key.
#}

with products as (
    select distinct
        product,
        sub_product
    from {{ ref('stg_cfpb__complaints') }}
)

select
    p.product || ' | ' || p.sub_product as product_nk,
    p.product,
    p.sub_product,
    coalesce(t.product_family, 'Unmapped') as product_family,
    coalesce(t.is_current_name, false) as is_current_name
from products as p
left join {{ ref('product_taxonomy') }} as t
    on p.product = t.product
