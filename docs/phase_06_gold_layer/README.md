# Phase 6 — Gold layer (Athena + Iceberg)

## Goal
Turn the silver tables into a **star schema** that analysts can query directly: one fact table (encounters) surrounded by dimensions (who, where, when), plus five KPI views. Everything is plain Athena SQL.

```
pf_silver (Parquet)                           pf_gold (Apache Iceberg)
  patient      --MERGE-->  dim_patient
  organization --MERGE-->  dim_facility
  practitioner --MERGE-->  dim_practitioner         five views on top of the fact table:
  (no source)  --CTAS --->  dim_date                  vw_daily_admissions_discharges
  encounter + condition --MERGE--> fact_encounter     vw_avg_los_by_facility_month
                                                      vw_readmission_30d
                                                      vw_ed_volume_by_hour_dow
                                                      vw_top_reasons
```

## What this phase creates
| Item | Purpose |
|---|---|
| `sql/gold/ddl/` (5 files) | One `CREATE TABLE` per gold table (`dim_date` is a `CREATE TABLE AS SELECT`) |
| `sql/gold/merge/` (4 files) | One `MERGE` per table that is loaded batch by batch |
| `sql/gold/views/` (5 files) | The KPI views |
| `infra/sql_files.py` | Plain-Python helpers: fill `{{PLACEHOLDERS}}`, split a file into statements, count `?` parameters, parse Athena results |
| `infra/aws_helpers.py` additions | `run_athena(..., parameters=)`, `run_sql_file(path, ingest_date=)` |
| `tests/test_sql_files.py` | 41 tests on the helper functions and on every SQL file |
| `notebooks/gold_layer.ipynb` | Builds everything on AWS and checks it against the source files |

## Concepts
- **Star schema**: a central *fact* table (one row per event, here per encounter) with numbers to measure, linked to small *dimension* tables (patient, facility, doctor, date) that describe it. Queries join the fact to the dimensions.
- **Grain**: what one fact row means. Here: one encounter (one visit or stay).
- **Apache Iceberg**: a table format that adds database features to files in S3: `MERGE`, row-level updates and deletes, snapshots, and partition management. Athena engine 3 supports it.
- **MERGE (upsert)**: for each source row, update the target row with the same key, or insert it if the key is new.
- **SCD type 1** (slowly changing dimension): when an attribute changes, the old value is overwritten; no history is kept.
- **Idempotent**: running the same merge again changes nothing.
- **Partition**: Iceberg splits the table's files by a value (here the year of the visit) so queries that filter on it read fewer files.
- **View**: a saved query; it stores no data and always reflects the current table.

## The tables
| Table | Key | Loaded by | Notes |
|---|---|---|---|
| `dim_patient` | `patient_id` | `merge_dim_patient.sql` | `age_years` and `age_group` (0-17, 18-34, 35-49, 50-64, 65+) are calculated on the day the merge runs; `is_deceased` `[EXTRA]` |
| `dim_facility` | `organization_id` | `merge_dim_facility.sql` | from the organization silver table |
| `dim_practitioner` | **`npi`** | `merge_dim_practitioner.sql` | keyed on the NPI because encounters reference doctors by NPI; `practitioner_id` is an attribute |
| `dim_date` | `date_key` | one-time CTAS | one row per day from **1900-01-01 to 2030-12-31** (47,847 rows): year, quarter, month, month name, day of month, day of week (1 = Monday), day name, is_weekend |
| `fact_encounter` | `encounter_id` | `merge_fact_encounter.sql` | partitioned by **`year(start_ts)`**; derived columns: `start_date`, `end_date`, `los_hours`, `los_days`, `is_inpatient` (class IMP), `is_emergency` (class EMER), `primary_condition_code` |

### How a MERGE is written (all four follow the same pattern)
1. **Source**: the silver rows of **one batch** only (`WHERE ingest_date = ?`). The `?` is an Athena *execution parameter*, so the same SQL runs for every batch and Step Functions can pass the date later.
2. **De-duplicate** the source with `ROW_NUMBER() ... WHERE rn = 1`, so no target row can be matched twice (which would make the MERGE fail).
3. `WHEN MATCHED THEN UPDATE` / `WHEN NOT MATCHED THEN INSERT`.
4. Order of loading: dimensions first, then the fact.

`primary_condition_code`: the code of the earliest condition recorded for the encounter (ties broken by condition id).
The organization and practitioner silver tables exist only for batch 1, so for batches 2–4 those two merges find an empty source and change nothing.

## The KPI views
| View | Definition |
|---|---|
| `vw_daily_admissions_discharges` | inpatient admissions counted on the stay's start date, discharges on its end date |
| `vw_avg_los_by_facility_month` | number, average and median length of stay (days) of inpatient stays, per facility and start month |
| `vw_readmission_30d` | an inpatient stay is a readmission if it starts within 30 days after the end of the same patient's previous inpatient stay (`LAG` over the patient's stays ordered by start); counted in the month and facility where it started; `readmission_rate = readmissions_30d / inpatient_admissions`; overlapping stays are not readmissions |
| `vw_ed_volume_by_hour_dow` | emergency visits by hour of day (UTC) and day of week |
| `vw_top_reasons` | encounters and patients per reason, ranked by volume |

## Results (`gold_layer.ipynb`, all checks green)
| Check | Result |
|---|---|
| Silver holds all 4 batches | batches 3 and 4 were ingested and transformed first (2 Lambda calls, 2 Glue runs) |
| Row counts vs distinct keys in silver | dim_patient 137, dim_facility 521, dim_practitioner 521, fact_encounter 4,586; dim_date 47,847 |
| Duplicate or null keys | none in any table |
| Encounter classes in the source | AMB 4,313; EMER 127; IMP 114; VR 18; HH 14 (total 4,586 = the fact table) |
| One encounter of each of IMP, EMER, AMB, field by field | start/end date, length of stay in hours, class flags, reason code and primary condition all equal what plain Python derives from the source files |
| Age | a patient's `age_years` and `age_group` equal a plain Python calculation; 14 deceased patients flagged = silver |
| Partitions | 82 yearly partitions = 82 distinct years with visits |
| **Rerun the merges for batches 1 and 2** | same row counts, still no duplicate keys |
| Facts without a dimension row | 0 without a patient, facility, doctor or date |
| Views | all five return rows (213, 106, 106, 78, 109) |
| Admissions and discharges | 114 each = the inpatient stays in the source |
| Emergency visits | 127 = the EMER encounters in the source |
| Average stay | 6.218 days, equal to the plain average over the source |
| Top reason | `431857002` with 498 encounters = source |
| **30-day readmissions** | **17 of 114 inpatient stays (14.9%)**, equal to an independent Python count straight from the source files |

## Problems found by testing on AWS (and fixed)
| # | Problem | Cause | Fix |
|---|---|---|---|
| 1 | `MERGE` failed: `ICEBERG_TOO_MANY_OPEN_PARTITIONS: Exceeded limit of 100 open writers` | The visits span **1924–2026**, so one batch has up to 352 different months; Athena writes at most 100 partitions per query | Partition by **year** (at most 71 per batch, 82 in total). A guard test stops anyone changing it back. A month per partition would also mean hundreds of tiny files for 4,586 rows. |
| 2 | Facts without a date row | the planned `dim_date` (2015–2027) did not cover visits back to 1924 | `dim_date` covers 1900–2030; the notebook rebuilds it if its size is not the expected one; guard test on the range |
| 3 | `run_athena` crashed with an `IndexError` on `CREATE TABLE` | statements that return no data have no result rows at all | the parser accepts an empty result (tested) |
| 4 | `dim_date` was "not found" and created twice | `SHOW TABLES` / `SHOW DATABASES` return **no header row**; the first table name was dropped as a header | the parser uses the statement type reported by Athena (only a query has a header row) (tested) |
| 5 | Glue: `ConcurrentRunsExceededException` | a job allows one run at a time, and the slot is released a few seconds after the run shows `SUCCEEDED` | `run_glue_job` waits and retries for up to 3 minutes. For the pipeline: two batches must not be started at the same time. |
| 6 | A check failed: metadata `record_count` 11,453 vs 4,586 rows | the check was wrong, not the data (see the next section) | the notebook compares partitions with years and reports both numbers |
| 7 | Iceberg `CREATE TABLE` rejected `varchar` | in DDL Athena uses Hive types (`string`, `int`, `double`, `boolean`, `date`, `timestamp`); in queries it uses Trino types (`varchar`, `integer`) | DDL files use Hive types |

## Iceberg behaviour worth knowing
An Iceberg `MERGE` does not edit rows in place. It writes new row versions and marks the old ones as deleted ("merge on read"). A query sees only the live rows (4,586), but the data files still hold the old versions (22,906 row versions after all the test merges; the number grows with every rerun) until the table is compacted. This costs storage and speed over time.
**Maintenance `[EXTRA]`, not done yet:** `OPTIMIZE pf_gold.fact_encounter REWRITE DATA USING BIN_PACK` merges small files and applies the deletes; `VACUUM pf_gold.fact_encounter` removes files that are no longer referenced. A scheduled maintenance step can be added to the pipeline later.
The partition names in `fact_encounter$partitions` look like `{start_ts_year=15}`: Iceberg's year transform counts years since 1970, so 15 means 1985.

## Things to remember
- **All times are UTC**, including the hour-of-day KPI.
- **Ages are as of the day the merge ran.** A patient not touched by a later batch keeps the age from when they were last merged.
- **Data oddities from the generator:** the visits go back to 1924 (not 5 years), and some "outpatient clinics" have inpatient stays. The top reasons are dominated by a few chronic patients (498 encounters for chronic kidney disease stage 4 from 3 patients). With only 4 batches the KPI numbers are small. The pipeline is correct; the data is synthetic.
- The gold tables were built from silver batches 1–4. The Step Functions pipeline (Phase 7) will run each batch end to end.

## Done when
- Gold tables populated for the batches. ✅
- Re-running the merges produces no duplicates. ✅
- All views return sensible results, confirmed against independent counts. ✅
