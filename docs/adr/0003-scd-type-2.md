# ADR 0003: SCD Type 2 for companies and products, with dbt snapshots over curated seeds

- **Status:** Accepted
- **Date:** 2026-11-14 (written up 2026-11-17)
- **Deciders:** Farhan Patel

## Context

Two dimensions have attributes that change over time, and the change matters for analysis:

- **Products.** The CFPB has renamed and regrouped its product taxonomy twice, in 2017 and
  2023. "Credit reporting" became "Credit reporting, credit repair services, or other
  personal consumer reports" and then "Credit reporting or other personal consumer reports".
  "Money transfers" and "Virtual currency" merged into one product. "Credit card or prepaid
  card" was split back into two. `seeds/product_taxonomy.csv` maps all 21 names, current and
  retired, to 12 stable families so trend lines survive. When I change which family a
  product belongs to, I want to know what the mapping was before.
- **Companies.** The CFPB publishes only a company name. Parent group and industry are my
  own curation (`seeds/company_groups.csv`), and curation gets corrected. A company I first
  file under "Payments and fintech" may later move to "Bank".

The source has no history of these attributes. The curated seeds are the only place they
change, so the warehouse has to record that history itself.

## Decision

- **dbt snapshots with the `check` strategy** over `int_company_attributes` and
  `int_product_attributes`, which combine the CFPB names with the seeds.
  - `snap_company` watches `parent_group` and `industry`.
  - `snap_product` watches `product_family` and `is_current_name`.

  When a watched column changes, the snapshot closes the current row and opens a new one.
  I use `check` rather than `timestamp` because no source column records when an attribute
  changed.
- **Dimensions built from the snapshots** (`dim_company`, `dim_product`):
  - **Half-open validity `[valid_from, valid_to)`.** The current row ends at 9999-12-31, so
    a date falls in exactly one version with `>= valid_from AND < valid_to`, with no
    `BETWEEN` double-counting at the boundary.
  - **First version backdated to 1900-01-01.** A snapshot stamps the first version with the
    time dbt first saw it (2026). Complaints go back to 2011, so without the backdating
    every historical complaint would fall before its company's first version and miss the
    join.
  - **Surrogate key = 64-bit hash of `(natural key, valid_from)`.** It identifies a version
    and stays stable across rebuilds, so facts never need re-keying when a dimension is
    rebuilt. An identity sequence would renumber on every full refresh.
  - **Unknown member `-1`** in every dimension, so a failed lookup is visible and countable
    rather than a silently dropped fact.
- **"As was" in the fact, "as is" in the marts.**
  - `fact_complaint` joins each complaint to the version valid on its `date_received`.
  - The marts re-join to the current version, so a dashboard groups the whole history by
    today's classification and a reclassification restates history consistently.

  Both views are available from the same fact.
- **A generic test, `scd2_valid_windows`**, asserts for every natural key: no gaps between
  versions, no overlaps, and exactly one current version. A bad snapshot or a hand edit then
  fails the build instead of duplicating facts through a double join.

## Alternatives considered

| Option | Why not |
|---|---|
| **Type 1 (overwrite)** | Simplest, and right for typo fixes, but a reclassification would silently rewrite every past report. That is exactly the history the product taxonomy needs. |
| **Type 3 (previous-value column)** | Keeps only one prior value; the product taxonomy has already changed twice. |
| **Type 6 / hybrid (current columns on every version)** | Gives "as is" and "as was" in one row, but every attribute change rewrites all of that key's versions. The marts get the same effect with a join to `is_current`. |
| **Hand-rolled SCD2 MERGE** | Possible in Postgres 15+, but dbt snapshots already handle change detection, closing rows and hard deletes, and they are tested. A custom MERGE would be code to own for no gain. |

## Consequences

- **`valid_from` is when dbt saw the change, not when it took effect.** Snapshots record the
  time of the run. For curated attributes that is the honest answer: the change *is* the
  curation. If business-effective dates mattered, the seed would need an `effective_from`
  column and the dimension would use it instead.
- **Snapshots are state.** They cannot be rebuilt from source. `snapshots.*` must be backed
  up with the database, and a full refresh of the warehouse must not drop them (the runbook
  covers this).
- **The current data has one version per key.** The seeds have not changed since the first
  snapshot: 8,214 company rows and 127 product rows, all current. The backdating, the
  as-was joins and `scd2_valid_windows` are what keep it correct once they do change. The
  runbook includes the steps to exercise a reclassification.
- **The fact joins on a range (`date_received` within the window).** With roughly 8k
  companies and 128 product versions, this is a cheap hash join on the natural key followed
  by a filter. It would need an interval index only with far more versions per key.
