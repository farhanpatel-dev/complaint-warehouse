-- Year-over-year trend per product family: each month against the same month a year
-- earlier, plus year-to-date against the prior year's same period. Last 24 months.
--
-- lag(complaints, 12) is only "the same month last year" if every family has a row for
-- every month, so the series is densified over a generated month calendar first.
WITH monthly AS (
    SELECT
        month,
        product_family,
        sum(complaints) AS complaints
    FROM marts.mv_product_monthly
    GROUP BY month, product_family
),

calendar AS (
    SELECT gs::DATE AS month
    FROM
        generate_series(
            (SELECT min(m.month) FROM monthly AS m),
            (SELECT max(m.month) FROM monthly AS m),
            INTERVAL '1 month'
        ) AS gs
),

families AS (
    SELECT DISTINCT product_family
    FROM monthly
),

dense AS (
    SELECT
        c.month,
        f.product_family,
        coalesce(m.complaints, 0) AS complaints
    FROM calendar AS c
    CROSS JOIN families AS f
    LEFT JOIN monthly AS m
        ON
            c.month = m.month
            AND f.product_family = m.product_family
),

trend AS (
    SELECT
        month,
        product_family,
        complaints,
        lag(complaints, 12) OVER fam AS complaints_prior_year,
        sum(complaints) OVER (
            PARTITION BY product_family, extract(YEAR FROM month)
            ORDER BY month
        ) AS ytd
    FROM dense
    WINDOW fam AS (PARTITION BY product_family ORDER BY month)
),

with_prior_ytd AS (
    SELECT
        *,
        lag(ytd, 12) OVER (PARTITION BY product_family ORDER BY month) AS ytd_prior_year
    FROM trend
)

SELECT
    month,
    product_family,
    complaints,
    complaints_prior_year,
    round(100.0 * (complaints - complaints_prior_year) / nullif(complaints_prior_year, 0), 1)
        AS yoy_pct,
    ytd,
    ytd_prior_year,
    round(100.0 * (ytd - ytd_prior_year) / nullif(ytd_prior_year, 0), 1) AS ytd_yoy_pct
FROM with_prior_ytd
WHERE month > (SELECT max(m.month) FROM monthly AS m) - INTERVAL '24 months'
ORDER BY product_family, month
