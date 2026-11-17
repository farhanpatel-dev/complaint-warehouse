-- Companies ranked by timely-response rate over the last 12 complete months.
--
-- The current month is excluded: its recent complaints are still "In progress" and have no
-- timely flag yet, which would drag every company's rate down.
--
-- Ranking by the raw rate rewards small samples (10 of 10 beats 9,950 of 10,000), so the
-- rank uses the lower bound of the 95% Wilson score interval instead: the rate we can be
-- confident a company achieves given how many complaints it had. Companies need at least
-- 500 complaints to appear at all.
WITH bounds AS (
    SELECT max(month) AS current_month
    FROM marts.mv_company_monthly
),

totals AS (
    SELECT
        m.company_name,
        m.industry,
        sum(m.complaints) AS complaints,
        sum(m.timely_responses) AS timely
    FROM marts.mv_company_monthly AS m
    CROSS JOIN bounds AS b
    WHERE
        m.month >= b.current_month - INTERVAL '12 months'
        AND m.month < b.current_month
    GROUP BY m.company_name, m.industry
    HAVING sum(m.complaints) >= 500
),

rates AS (
    SELECT
        company_name,
        industry,
        complaints,
        timely::NUMERIC / complaints AS p,
        complaints::NUMERIC AS n
    FROM totals
),

wilson AS (
    -- z = 1.96 for a 95% interval; 3.8416 = z^2.
    SELECT
        company_name,
        industry,
        complaints,
        p,
        (p + 3.8416 / (2 * n) - 1.96 * sqrt(p * (1 - p) / n + 3.8416 / (4 * n * n)))
        / (1 + 3.8416 / n) AS wilson_lower
    FROM rates
)

SELECT
    company_name,
    industry,
    complaints,
    round(p, 4) AS timely_rate,
    round(wilson_lower, 4) AS timely_rate_lower_95,
    rank() OVER (ORDER BY wilson_lower DESC) AS overall_rank,
    rank() OVER (PARTITION BY industry ORDER BY wilson_lower DESC) AS industry_rank,
    ntile(4) OVER (ORDER BY wilson_lower DESC) AS quartile
FROM wilson
ORDER BY overall_rank, company_name
