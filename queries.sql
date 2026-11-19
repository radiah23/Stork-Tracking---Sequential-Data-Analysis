-- queries.sql: SQL questions about the stork tracking data (table: tracking)
-- Each query starts with a comment line "-- Cn: question" (used as its title
-- by run_queries.py) and ends with a semicolon.

-- C1: How many GPS points do we have per stork?
SELECT
    animal_id,
    COUNT(*) AS n_points          -- COUNT(*) = number of rows in each group
FROM tracking
GROUP BY animal_id                -- one result row per stork
ORDER BY n_points DESC;           -- most points first

-- C2: When did tracking start and end for each stork (tracking period)?
SELECT
    animal_id,
    MIN(timestamp) AS first_fix,     -- earliest time = start of tracking
    MAX(timestamp) AS last_fix,      -- latest time   = end of tracking
    -- julianday() turns a date into a number of days, so subtracting two
    -- of them gives the length of the period in days
    ROUND(julianday(MAX(timestamp)) - julianday(MIN(timestamp)), 1) AS days_tracked
FROM tracking
GROUP BY animal_id
ORDER BY days_tracked;               -- shortest tracks first

-- C3: What is the southernmost latitude each stork reached?
-- (Lower latitude = further south. ~47.7 = southern Germany,
--  ~36 = Strait of Gibraltar, < 36 = Africa.)
SELECT
    animal_id,
    ROUND(MAX(latitude), 2) AS northernmost_lat,
    ROUND(MIN(latitude), 2) AS southernmost_lat,
    -- SQLite-only shortcut: when a query uses a single MIN() or MAX(),
    -- other plain columns come from the SAME row as that min/max.
    -- So longitude and timestamp here belong to the southernmost point.
    -- (Other databases don't allow this; they'd need a window function.)
    ROUND(longitude, 2)     AS lon_at_southernmost,
    timestamp               AS reached_at,
    ROUND(MAX(latitude) - MIN(latitude), 2) AS degrees_south
FROM tracking
GROUP BY animal_id
ORDER BY southernmost_lat;        -- furthest south first

-- C4: How far did each stork fly in total (and per day tracked)?
SELECT
    animal_id,
    ROUND(SUM(step_km), 0) AS total_km,         -- add up every step
    ROUND(julianday(MAX(timestamp)) - julianday(MIN(timestamp)), 1) AS days_tracked,
    -- km per day makes storks with short tracks comparable
    ROUND(SUM(step_km) / (julianday(MAX(timestamp)) - julianday(MIN(timestamp))), 1) AS km_per_day
FROM tracking
WHERE speed_kmh <= 120         -- skip GPS-error jumps (impossible speeds)
   OR speed_kmh IS NULL        -- keep each stork's first point (no speed yet)
GROUP BY animal_id
ORDER BY total_km DESC;

-- C5: What is the average and maximum speed per stork?
-- Speeds above 120 km/h are GPS errors (storks fly ~40-50 km/h), so we drop them.
-- avg_moving_kmh only counts steps faster than 12 km/h (actually flying),
-- because the plain average is dominated by hours of resting.
SELECT
    animal_id,
    ROUND(AVG(speed_kmh), 1) AS avg_kmh,
    ROUND(AVG(CASE WHEN speed_kmh > 12 THEN speed_kmh END), 1) AS avg_moving_kmh,
    ROUND(MAX(speed_kmh), 1) AS max_kmh
FROM tracking
WHERE speed_kmh <= 120
GROUP BY animal_id
ORDER BY max_kmh DESC
LIMIT 10;

-- C6: Window function LAG: recompute lat_change in SQL (sample rows)
-- LAG(latitude) = latitude of the previous row
-- PARTITION BY animal_id = restart for each stork (like pandas groupby)
-- ORDER BY timestamp     = "previous" means previous in time (like our sort)
-- IMPORTANT: WHERE runs BEFORE window functions. So we compute LAG over the
-- full table inside a CTE (WITH ...) first, and only filter afterwards.
-- Otherwise the first filtered row would have no "previous" row -> NULL.
WITH lagged AS (
    SELECT
        animal_id,
        timestamp,
        latitude,
        LAG(latitude) OVER (PARTITION BY animal_id ORDER BY timestamp) AS prev_lat_sql,
        lat_change AS lat_change_pandas
    FROM tracking
)
SELECT
    animal_id,
    timestamp,
    latitude,
    prev_lat_sql,
    latitude - prev_lat_sql AS lat_change_sql,
    lat_change_pandas
FROM lagged
WHERE animal_id LIKE 'Ekky%' AND timestamp >= '2014-09-01 11:00'
LIMIT 5;

-- C6b: Check: does the SQL LAG result match the pandas groupby+shift result on ALL rows?
WITH lagged AS (
    SELECT
        lat_change AS pandas_value,
        latitude - LAG(latitude) OVER (PARTITION BY animal_id ORDER BY timestamp) AS sql_value
    FROM tracking
)
SELECT
    COUNT(*)                                            AS total_rows,
    SUM(sql_value IS NULL)                              AS first_points_null,
    SUM(ABS(sql_value - pandas_value) > 0.0000001)      AS mismatches
FROM lagged;

-- C7: Which were the biggest single days of travel (km per stork per day)?
-- gap_hours <= 1: skip steps that follow a long tag silence. Example: after a
-- 5-day gap, one 1,097 km step would otherwise count as a single day's flight.
SELECT
    animal_id,
    DATE(timestamp)        AS day,           -- cut the time off: '2014-09-01'
    ROUND(SUM(step_km), 0) AS km_that_day
FROM tracking
WHERE speed_kmh <= 120 AND gap_hours <= 1
GROUP BY animal_id, DATE(timestamp)          -- one row per stork per day
ORDER BY km_that_day DESC
LIMIT 10;

-- C8: Running total: cumulative distance of the top stork (Ekky), day by day
-- SUM(...) OVER (... ORDER BY day) adds up everything UP TO the current row,
-- so each row keeps its own day AND shows the total so far.
WITH daily AS (
    SELECT animal_id, DATE(timestamp) AS day, SUM(step_km) AS km
    FROM tracking
    WHERE animal_id LIKE 'Ekky%' AND speed_kmh <= 120
    GROUP BY animal_id, DATE(timestamp)
),
running AS (                      -- running total over ALL days first...
    SELECT
        day,
        km,
        SUM(km) OVER (PARTITION BY animal_id ORDER BY day) AS cumulative_km
    FROM daily
)
SELECT                            -- ...then show only a few sample days
    day,
    ROUND(km, 0)            AS km_that_day,
    ROUND(cumulative_km, 0) AS cumulative_km
FROM running
WHERE day IN ('2014-08-01','2014-08-15','2014-08-25','2014-09-01','2014-09-05',
              '2014-09-10','2014-09-15','2014-09-20','2014-10-01');

-- C9: Stopovers: where did storks rest for 3+ days in a row (< 5 km per day)?
-- Classic "gaps and islands" trick for sequences:
-- for consecutive rest days, (day number - row number) stays the same,
-- so it works as an ID for each unbroken stretch of rest days.
WITH daily AS (
    SELECT animal_id, DATE(timestamp) AS day,
           SUM(step_km) AS km, AVG(latitude) AS lat, AVG(longitude) AS lon
    FROM tracking
    WHERE speed_kmh <= 120
    GROUP BY animal_id, DATE(timestamp)
),
rest_days AS (
    SELECT *,
           julianday(day)
             - ROW_NUMBER() OVER (PARTITION BY animal_id ORDER BY day) AS stretch_id
    FROM daily
    WHERE km < 5
)
SELECT
    animal_id,
    MIN(day)            AS from_day,
    MAX(day)            AS to_day,
    COUNT(*)            AS rest_days,
    ROUND(AVG(lat), 2)  AS lat,
    ROUND(AVG(lon), 2)  AS lon
FROM rest_days
GROUP BY animal_id, stretch_id
HAVING COUNT(*) >= 3                 -- HAVING = WHERE, but applied after grouping
   AND AVG(lat) < 46                 -- only stopovers away from home (left Germany)
ORDER BY rest_days DESC
LIMIT 10;

-- C10: Rank the storks by total distance flown
WITH totals AS (
    SELECT animal_id, SUM(step_km) AS total_km
    FROM tracking
    WHERE speed_kmh <= 120
    GROUP BY animal_id
)
SELECT
    RANK() OVER (ORDER BY total_km DESC) AS rank,   -- 1 = farthest
    animal_id,
    ROUND(total_km, 0) AS total_km
FROM totals
ORDER BY rank
LIMIT 10;
