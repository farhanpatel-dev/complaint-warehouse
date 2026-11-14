{#
    dbt's default prefixes custom schemas with the target schema (warehouse_staging, ...).
    The schemas here are fixed layers created by db/init, so I use the custom name as-is.
#}
{% macro generate_schema_name(custom_schema_name, node) -%}
    {%- if custom_schema_name is none -%}
        {{ target.schema }}
    {%- else -%}
        {{ custom_schema_name | trim }}
    {%- endif -%}
{%- endmacro %}
