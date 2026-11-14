{#
    Consumer narratives with whitespace normalised.

    The CFPB redacts personal data as runs of X ("XXXX", "XX/XX/XXXX"). narrative_clean drops
    those tokens: to full-text search and to an embedding model they are noise that makes
    unrelated complaints look similar. The original text stays in `narrative` for display.
#}

with source as (
    select * from {{ source('cfpb', 'complaint_narratives') }}
),

normalised as (
    select
        complaint_id,
        date_received,
        regexp_replace(trim(narrative), '\s+', ' ', 'g') as narrative,
        last_changed_at
    from source
)

select
    complaint_id,
    date_received,
    narrative,
    trim(regexp_replace(
        regexp_replace(narrative, '\mX{2,}(/X{2,})*\M', ' ', 'g'),
        '\s+', ' ', 'g'
    )) as narrative_clean,
    length(narrative) as narrative_chars,
    last_changed_at
from normalised
