{#
    hash_key: deterministic 64-bit surrogate key.

    I take the first 16 hex digits of an MD5 over the natural-key columns and cast them to
    BIGINT. Compared with sequences, the key is the same on every rebuild, so an incremental
    fact never points at a key that a dimension rebuild renumbered. Compared with a 32-char
    MD5 text key, it is 8 bytes instead of 33+, which matters on a 19M-row fact and its
    indexes. Collision odds for n keys are about n^2 / 2^65: roughly 1e-11 for 20k dimension
    rows.
#}
{% macro hash_key(columns) -%}
    ('x' || substr(md5(
        {%- for col in columns %}
        coalesce(cast({{ col }} as text), '~'){% if not loop.last %} || '|' ||{% endif %}
        {%- endfor %}
    ), 1, 16))::bit(64)::bigint
{%- endmacro %}

{# date_key: YYYYMMDD integer, the conventional smart key for a date dimension. #}
{% macro date_key(column) -%}
    (extract(year from {{ column }})::int * 10000
     + extract(month from {{ column }})::int * 100
     + extract(day from {{ column }})::int)
{%- endmacro %}

{# The member every dimension carries for facts whose lookup fails. #}
{% macro unknown_key() -%}
    -1::bigint
{%- endmacro %}
