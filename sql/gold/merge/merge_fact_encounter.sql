-- Upsert the encounters of ONE batch (BOTH ? are the ingest_date) into gold.fact_encounter.
-- Derived columns:
--   start_date / end_date   the day of the start / end time (UTC)
--   los_hours / los_days    length of stay: end minus start, in hours / in days
--   is_inpatient            class IMP (a hospital stay);  is_emergency  class EMER
--   primary_condition_code  the code of the earliest condition recorded for the encounter (ties broken by id)
-- Conditions and encounters of a patient arrive in the same batch, so the condition source uses the same ingest_date.
-- Run the dimension merges first, then this one.
MERGE INTO pf_gold.fact_encounter AS t
USING (
  SELECT
    e.encounter_id, e.patient_id, e.organization_id, e.practitioner_npi, e.encounter_class,
    e.start_ts, e.end_ts,
    CAST(e.start_ts AS date) AS start_date,
    CAST(e.end_ts AS date) AS end_date,
    CAST(date_diff('second', e.start_ts, e.end_ts) AS double) / 3600 AS los_hours,
    CAST(date_diff('second', e.start_ts, e.end_ts) AS double) / 86400 AS los_days,
    e.encounter_class = 'IMP' AS is_inpatient,
    e.encounter_class = 'EMER' AS is_emergency,
    e.reason_code, e.reason_display,
    c.code AS primary_condition_code
  FROM (
    SELECT *, ROW_NUMBER() OVER (PARTITION BY encounter_id ORDER BY ingest_date DESC) AS rn
    FROM pf_silver.encounter
    WHERE ingest_date = ?
  ) AS e
  LEFT JOIN (
    SELECT encounter_id, code,
           ROW_NUMBER() OVER (PARTITION BY encounter_id ORDER BY onset_ts, condition_id) AS rn
    FROM pf_silver.condition
    WHERE ingest_date = ?
  ) AS c
    ON c.encounter_id = e.encounter_id AND c.rn = 1
  WHERE e.rn = 1
) AS s
ON t.encounter_id = s.encounter_id
WHEN MATCHED THEN UPDATE SET
  patient_id = s.patient_id, organization_id = s.organization_id, practitioner_npi = s.practitioner_npi,
  encounter_class = s.encounter_class, start_ts = s.start_ts, end_ts = s.end_ts,
  start_date = s.start_date, end_date = s.end_date, los_hours = s.los_hours, los_days = s.los_days,
  is_inpatient = s.is_inpatient, is_emergency = s.is_emergency, reason_code = s.reason_code,
  reason_display = s.reason_display, primary_condition_code = s.primary_condition_code
WHEN NOT MATCHED THEN INSERT (
  encounter_id, patient_id, organization_id, practitioner_npi, encounter_class, start_ts, end_ts,
  start_date, end_date, los_hours, los_days, is_inpatient, is_emergency, reason_code, reason_display,
  primary_condition_code
) VALUES (
  s.encounter_id, s.patient_id, s.organization_id, s.practitioner_npi, s.encounter_class, s.start_ts, s.end_ts,
  s.start_date, s.end_date, s.los_hours, s.los_days, s.is_inpatient, s.is_emergency, s.reason_code, s.reason_display,
  s.primary_condition_code
)
