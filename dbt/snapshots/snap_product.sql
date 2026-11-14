{#
    SCD Type 2 history for products. A new version opens when the product family mapping
    changes (an analyst regroups products, or the CFPB retires a product name).
#}
{% snapshot snap_product %}
    {{
        config(
            target_schema='snapshots',
            unique_key='product_nk',
            strategy='check',
            check_cols=['product_family', 'is_current_name'],
        )
    }}

    select * from {{ ref('int_product_attributes') }}

{% endsnapshot %}
