-- Gold dimension: one row per calendar day from 1900-01-01 to 2030-12-31, built once.
-- (The range must cover every date in the data: the visits go back to 1924, so the plan's 2015-2027 was too short.)
-- sequence() produces the days, UNNEST turns them into rows. CAST makes date_key a real date (without it Athena stores timestamps). day_of_week: 1 = Monday ... 7 = Sunday.
CREATE TABLE pf_gold.dim_date
WITH (
  table_type = 'ICEBERG',
  is_external = false,
  location = 's3://{{LAKE_BUCKET}}/gold/dim_date/',
  format = 'PARQUET'
) AS
SELECT
  CAST(d AS date)                     AS date_key,
  year(d)                             AS year,
  quarter(d)                          AS quarter,
  month(d)                            AS month,
  date_format(d, '%M')                AS month_name,
  day(d)                              AS day_of_month,
  day_of_week(d)                      AS day_of_week,
  date_format(d, '%W')                AS day_name,
  day_of_week(d) IN (6, 7)            AS is_weekend
FROM UNNEST(sequence(DATE '1900-01-01', DATE '2030-12-31', INTERVAL '1' DAY)) AS t(d)
