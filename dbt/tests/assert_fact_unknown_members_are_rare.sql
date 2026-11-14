-- Facts falling back to the -1 unknown member mean a dimension lookup failed. A handful is
-- tolerable; more than 0.1% means a join key drifted between staging and a dimension.
{{ config(severity='warn') }}

select
    count(*) filter (where company_key = -1) as unknown_company,
    count(*) filter (where product_key = -1) as unknown_product,
    count(*) filter (where geo_key = -1) as unknown_geo,
    count(*) as total
from {{ ref('fact_complaint') }}
having
    count(*) filter (where company_key = -1 or product_key = -1 or geo_key = -1)
    > 0.001 * count(*)
