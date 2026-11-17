-- Rolling 30-day complaint volume and timely-response rate per product family, last 365 days.
--
-- The rate is sum(timely) / sum(complaints) over the window, not an average of daily rates:
-- averaging ratios would give a quiet day with 3 complaints the same weight as a busy day
-- with 3,000.
--
-- The series is densified against dim_date first. A ROWS frame counts rows, not days, so
-- a family with no complaints on some day would otherwise get a "30-day" window that
-- silently spans more than 30 calendar days.
WITH bounds AS (
    SELECT max(date_received) AS last_day
    FROM marts.mv_daily_volume
),

families AS (
    SELECT DISTINCT product_family
    FROM marts.mv_daily_volume
),

daily AS (
    SELECT
        v.date_received,
        v.product_family,
        sum(v.complaints) AS complaints,
        sum(v.timely_responses) AS timely_responses
    FROM marts.mv_daily_volume AS v
    CROSS JOIN bounds AS b
    WHERE v.date_received > b.last_day - (365 + 29)
    GROUP BY v.date_received, v.product_family
),

dense AS (
    SELECT
        d.date_day,
        f.product_family,
        coalesce(daily.complaints, 0) AS complaints,
        coalesce(daily.timely_responses, 0) AS timely_responses
    FROM warehouse.dim_date AS d
    CROSS JOIN bounds AS b
    CROSS JOIN families AS f
    LEFT JOIN daily
        ON
            d.date_day = daily.date_received
            AND f.product_family = daily.product_family
    WHERE d.date_day > b.last_day - (365 + 29) AND d.date_day <= b.last_day
),

rolling AS (
    SELECT
        date_day,
        product_family,
        complaints,
        sum(complaints) OVER w30 AS complaints_30d,
        sum(timely_responses) OVER w30 AS timely_30d
    FROM dense
    WINDOW w30 AS (
        PARTITION BY product_family
        ORDER BY date_day
        ROWS BETWEEN 29 PRECEDING AND CURRENT ROW
    )
)

SELECT
    r.date_day,
    r.product_family,
    r.complaints,
    r.complaints_30d,
    round(r.timely_30d::NUMERIC / nullif(r.complaints_30d, 0), 4) AS timely_rate_30d
FROM rolling AS r
CROSS JOIN bounds AS b
WHERE r.date_day > b.last_day - 365
ORDER BY r.product_family, r.date_day
