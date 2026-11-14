{#
    One row per complaint, typed and cleaned. This is the only place source quirks are
    handled, so every downstream model sees the same conventions:

      * Dimension natural keys are never NULL. A missing value becomes an explicit sentinel,
        so fact-to-dimension joins can be plain hash joins (IS NOT DISTINCT FROM cannot be
        hashed, and on 19M rows that is the difference between seconds and hours).
      * Yes/No text becomes boolean; anything else becomes NULL rather than a guess.
      * ZIP codes keep CFPB masking (e.g. '391XX'); zip3 is derived for both forms.
#}

with source as (
    select * from {{ source('cfpb', 'complaints') }}
),

cleaned as (
    select
        complaint_id,
        date_received,
        date_sent_to_company,

        coalesce(nullif(regexp_replace(trim(company), '\s+', ' ', 'g'), ''), 'Unknown')
            as company_name,
        coalesce(nullif(trim(product), ''), 'Unknown') as product,
        coalesce(nullif(trim(sub_product), ''), '(none)') as sub_product,
        nullif(trim(issue), '') as issue,
        nullif(trim(sub_issue), '') as sub_issue,

        case
            when upper(trim(state)) ~ '^[A-Z]{2}$' then upper(trim(state))
            when upper(trim(state)) = 'UNITED STATES MINOR OUTLYING ISLANDS' then 'UM'
            else '--'
        end as state_code,
        case
            when upper(trim(zip_code)) ~ '^[0-9]{5}' then left(trim(zip_code), 5)
            when upper(trim(zip_code)) ~ '^[0-9]{3}XX$' then upper(trim(zip_code))
            else '-----'
        end as zip_code,

        nullif(trim(tags), '') as tags,
        nullif(trim(submitted_via), '') as submitted_via,
        coalesce(nullif(trim(company_response), ''), 'Unknown') as response_type,
        nullif(trim(company_public_response), '') as company_public_response,

        case lower(trim(timely)) when 'yes' then true when 'no' then false end as timely_flag,
        case lower(trim(consumer_disputed))
            when 'yes' then true
            when 'no' then false
        end as disputed_flag,

        last_changed_at
    from source
)

select
    *,
    case when zip_code ~ '^[0-9]{3}' then left(zip_code, 3) end as zip3,
    zip_code like '%XX' as is_zip_masked,
    coalesce(tags like '%Older American%', false) as is_older_american,
    coalesce(tags like '%Servicemember%', false) as is_servicemember,
    date_sent_to_company - date_received as days_to_company
from cleaned
