-- A product name missing from the product_taxonomy seed lands in family 'Unmapped' and
-- disappears from family-level trends. Warn so a new CFPB product name gets a seed row.
{{ config(severity='warn') }}

select distinct product
from {{ ref('dim_product') }}
where product_family = 'Unmapped'
