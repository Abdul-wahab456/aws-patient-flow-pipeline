-- KPI: emergency visits by hour of the day and day of the week (all times are UTC).
-- day_of_week: 1 = Monday ... 7 = Sunday.
CREATE OR REPLACE VIEW pf_gold.vw_ed_volume_by_hour_dow AS
SELECT
  hour(start_ts) AS hour_of_day_utc,
  day_of_week(start_ts) AS day_of_week,
  date_format(start_ts, '%W') AS day_name,
  COUNT(*) AS emergency_visits
FROM pf_gold.fact_encounter
WHERE is_emergency
GROUP BY hour(start_ts), day_of_week(start_ts), date_format(start_ts, '%W')
