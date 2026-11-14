{#
    partitioned_incremental: an incremental materialization for Postgres declarative
    range partitioning by month.

    dbt-postgres builds tables with CREATE TABLE AS, which cannot declare PARTITION BY, and a
    full refresh would replace a partitioned table with a plain one. This materialization:

      1. stages the model's rows in a temp table
      2. on first build, creates the parent as LIKE <temp> PARTITION BY RANGE (<column>)
      3. creates any missing monthly partitions for the months present in the batch
      4. delete+insert on unique_key (deletes hit every partition through the index that
         starts with unique_key; inserts are routed to partitions by Postgres)

    Config:
      partition_column  date/timestamp column to range-partition on (required)
      unique_key        column identifying a row (required)

    Inside the model, use cw_is_incremental() instead of is_incremental(): the built-in
    only returns true for materialized='incremental'.

    A schema change needs --full-refresh, which drops and rebuilds the parent and all its
    partitions from staging.
#}
{% materialization partitioned_incremental, adapter='postgres' %}
    {%- set partition_column = config.require('partition_column') -%}
    {%- set unique_key = config.require('unique_key') -%}
    {%- set target_relation = this.incorporate(type='table') -%}
    {%- set existing_relation = load_cached_relation(this) -%}
    {%- set tmp_relation = make_temp_relation(target_relation) -%}

    {{ run_hooks(pre_hooks, inside_transaction=False) }}
    {{ run_hooks(pre_hooks, inside_transaction=True) }}

    {% if existing_relation is not none and should_full_refresh() %}
        {% do adapter.drop_relation(existing_relation) %}
        {% set existing_relation = none %}
    {% endif %}

    {% call statement('stage_batch') -%}
        {{ get_create_table_as_sql(True, tmp_relation, sql) }}
    {%- endcall %}

    {% if existing_relation is none %}
        {% call statement('create_parent') -%}
            create table {{ target_relation }} (like {{ tmp_relation }} including defaults)
            partition by range ({{ partition_column }})
        {%- endcall %}
    {% endif %}

    {% call statement('ensure_partitions') -%}
        {{ cw_create_month_partitions(target_relation, tmp_relation, partition_column) }}
    {%- endcall %}

    {% call statement('main') -%}
        delete from {{ target_relation }} as t
        using {{ tmp_relation }} as s
        where t.{{ unique_key }} = s.{{ unique_key }};

        insert into {{ target_relation }}
        select * from {{ tmp_relation }};
    {%- endcall %}

    {{ run_hooks(post_hooks, inside_transaction=True) }}
    {% do adapter.commit() %}
    {{ run_hooks(post_hooks, inside_transaction=False) }}

    {% do persist_docs(target_relation, model) %}
    {{ return({'relations': [target_relation]}) }}
{% endmaterialization %}


{% macro cw_create_month_partitions(parent, source, column) %}
    do $$
    declare
        m date;
    begin
        for m in
            select distinct date_trunc('month', {{ column }})::date
            from {{ source }}
            where {{ column }} is not null
        loop
            execute format(
                'create table if not exists %I.%I partition of %I.%I for values from (%L) to (%L)',
                '{{ parent.schema }}', '{{ parent.identifier }}_' || to_char(m, 'YYYY_MM'),
                '{{ parent.schema }}', '{{ parent.identifier }}',
                m, (m + interval '1 month')::date
            );
        end loop;
    end
    $$;
{% endmacro %}


{% macro cw_is_incremental() %}
    {%- if not execute -%}
        {{ return(false) }}
    {%- endif -%}
    {%- set relation = adapter.get_relation(this.database, this.schema, this.identifier) -%}
    {{ return(relation is not none and not should_full_refresh()) }}
{% endmacro %}
