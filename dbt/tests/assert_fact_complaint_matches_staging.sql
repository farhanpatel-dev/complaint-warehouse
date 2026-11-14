-- Every staged complaint lands in the fact exactly once. Catches dropped rows (a failed
-- dimension join on an inner path) and fan-out (overlapping SCD2 windows, duplicate geo
-- keys), which a unique test on complaint_id alone would only half cover.
with counts as (
    select
        (select count(*) from {{ ref('stg_cfpb__complaints') }}) as staged,
        (select count(*) from {{ ref('fact_complaint') }}) as facts
)

select *
from counts
where staged <> facts
