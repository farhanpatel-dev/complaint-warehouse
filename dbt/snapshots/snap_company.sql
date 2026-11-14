{#
    SCD Type 2 history for companies. The check strategy compares the listed columns on each
    run and, when one changes, closes the current row (dbt_valid_to = now) and opens a new
    one. The source has no reliable "updated_at" for company attributes, so check beats the
    timestamp strategy here.
#}
{% snapshot snap_company %}
    {{
        config(
            target_schema='snapshots',
            unique_key='company_name',
            strategy='check',
            check_cols=['parent_group', 'industry'],
        )
    }}

    select * from {{ ref('int_company_attributes') }}

{% endsnapshot %}
