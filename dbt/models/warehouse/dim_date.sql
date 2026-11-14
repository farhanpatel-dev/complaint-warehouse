{{ config(
    post_hook="create unique index if not exists dim_date_date_idx on {{ this }} (date_day)"
) }}

with spine as (
    select generate_series(
        '{{ var("date_spine_start") }}'::date,
        '{{ var("date_spine_end") }}'::date,
        interval '1 day'
    )::date as date_day
)

select
    {{ date_key('date_day') }} as date_key,
    date_day,
    extract(year from date_day)::int as year,
    extract(quarter from date_day)::int as quarter,
    extract(month from date_day)::int as month,
    to_char(date_day, 'FMMonth') as month_name,
    date_trunc('month', date_day)::date as month_start,
    date_trunc('quarter', date_day)::date as quarter_start,
    extract(isoyear from date_day)::int as iso_year,
    extract(week from date_day)::int as iso_week,
    date_trunc('week', date_day)::date as week_start,
    extract(isodow from date_day)::int as iso_day_of_week,
    to_char(date_day, 'FMDay') as day_name,
    extract(isodow from date_day) in (6, 7) as is_weekend,
    extract(day from date_day)::int as day_of_month,
    extract(doy from date_day)::int as day_of_year,
    (date_trunc('month', date_day) + interval '1 month - 1 day')::date = date_day as is_month_end
from spine
