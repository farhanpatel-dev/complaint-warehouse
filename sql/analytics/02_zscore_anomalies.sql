-- Company-day complaint spikes in the last 365 days: z-score against a trailing 28-day baseline.
--
--   z = (complaints today - mean of the previous 28 days) / stddev of the previous 28 days
--
-- The baseline window ends the day before ("1 PRECEDING"), so a spike cannot inflate its
-- own mean and standard deviation and hide itself. Days are densified with zeros (a quiet
-- day is information, not a gap), and only companies with at least 1,000 complaints in the
-- period are scored, which keeps the dense series small and the baseline meaningful.
-- A minimum of 20 complaints on the day stops a jump from 0 to 3 being called an anomaly.
--
-- Known limit: weekends are quieter, which widens the baseline's standard deviation.
-- A same-weekday baseline would be sharper at the cost of only 4 points per window.
WITH bounds AS (
    SELECT max(date_received) AS last_day
    FROM marts.mv_daily_volume
),

company_daily AS (
    SELECT
        v.date_received,
        v.company_name,
        sum(v.complaints) AS complaints
    FROM marts.mv_daily_volume AS v
    CROSS JOIN bounds AS b
    WHERE v.date_received > b.last_day - (365 + 28)
    GROUP BY v.date_received, v.company_name
),

active AS (
    SELECT company_name
    FROM company_daily
    GROUP BY company_name
    HAVING sum(complaints) >= 1000
),

dense AS (
    SELECT
        d.date_day,
        a.company_name,
        coalesce(c.complaints, 0) AS complaints
    FROM warehouse.dim_date AS d
    CROSS JOIN bounds AS b
    CROSS JOIN active AS a
    LEFT JOIN company_daily AS c
        ON
            d.date_day = c.date_received
            AND a.company_name = c.company_name
    WHERE d.date_day > b.last_day - (365 + 28) AND d.date_day <= b.last_day
),

scored AS (
    SELECT
        date_day,
        company_name,
        complaints,
        avg(complaints) OVER baseline AS mean_28d,
        stddev_samp(complaints) OVER baseline AS stddev_28d
    FROM dense
    WINDOW baseline AS (
        PARTITION BY company_name
        ORDER BY date_day
        ROWS BETWEEN 28 PRECEDING AND 1 PRECEDING
    )
)

SELECT
    s.date_day,
    s.company_name,
    s.complaints,
    round(s.mean_28d, 1) AS mean_28d,
    round(s.stddev_28d, 1) AS stddev_28d,
    round((s.complaints - s.mean_28d) / nullif(s.stddev_28d, 0), 1) AS z_score
FROM scored AS s
CROSS JOIN bounds AS b
WHERE
    s.date_day > b.last_day - 365
    AND s.complaints >= 20
    AND (s.complaints - s.mean_28d) / nullif(s.stddev_28d, 0) >= 4
ORDER BY z_score DESC, s.date_day ASC, s.company_name ASC
LIMIT 50
