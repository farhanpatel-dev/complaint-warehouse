{#
    Generic test for an SCD Type 2 dimension. Returns one row per violation:
      * overlap or gap: a version's valid_to is not the next version's valid_from
      * not exactly one current version per natural key
      * an empty or inverted window (valid_from >= valid_to)

    Usage (model level):
      data_tests:
        - scd2_valid_windows:
            natural_key: company_name
#}
{% test scd2_valid_windows(model, natural_key, valid_from='valid_from', valid_to='valid_to',
                           is_current='is_current') %}

with ordered as (
    select
        {{ natural_key }} as natural_key,
        {{ valid_from }} as valid_from,
        {{ valid_to }} as valid_to,
        {{ is_current }} as is_current,
        lead({{ valid_from }}) over (
            partition by {{ natural_key }} order by {{ valid_from }}
        ) as next_valid_from
    from {{ model }}
),

window_errors as (
    select natural_key, 'gap_or_overlap' as violation
    from ordered
    where next_valid_from is not null and next_valid_from <> valid_to

    union all

    select natural_key, 'empty_window' as violation
    from ordered
    where valid_from >= valid_to
),

current_errors as (
    select natural_key, 'current_count_' || count(*) filter (where is_current) as violation
    from ordered
    group by natural_key
    having count(*) filter (where is_current) <> 1
)

select * from window_errors
union all
select * from current_errors

{% endtest %}
