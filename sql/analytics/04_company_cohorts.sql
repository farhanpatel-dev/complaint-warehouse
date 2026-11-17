-- Company cohorts: companies grouped by the year of their first complaint, and how many of
-- each cohort still receive complaints 1, 2, ... years later ("retention"), with volume.
--
-- It answers questions like "do companies that enter the database keep generating
-- complaints, or is it mostly a one-off?" and "is the 2020 cohort bigger than 2015's?".
--
-- The 2011 cohort is left-censored: the data starts in December 2011, so it also holds
-- every company that already existed then, not only newcomers. The latest year is partial.
WITH company_year AS (
    SELECT
        company_name,
        extract(YEAR FROM month)::INTEGER AS yr,
        sum(complaints) AS complaints
    FROM marts.mv_company_monthly
    GROUP BY company_name, extract(YEAR FROM month)
),

firsts AS (
    SELECT
        company_name,
        min(yr) AS cohort_year
    FROM company_year
    GROUP BY company_name
),

activity AS (
    SELECT
        f.cohort_year,
        c.yr - f.cohort_year AS years_since,
        count(*) AS active_companies,
        sum(c.complaints) AS complaints
    FROM company_year AS c
    INNER JOIN firsts AS f ON c.company_name = f.company_name
    GROUP BY f.cohort_year, c.yr - f.cohort_year
)

SELECT
    cohort_year,
    years_since,
    active_companies,
    round(
        active_companies::NUMERIC
        / first_value(active_companies) OVER (PARTITION BY cohort_year ORDER BY years_since),
        3
    ) AS retention,
    complaints
FROM activity
ORDER BY cohort_year, years_since
