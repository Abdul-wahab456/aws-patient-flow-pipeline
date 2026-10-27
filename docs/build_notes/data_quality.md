# Data quality checks and KPI charts

## Goal
The pipeline should **check its own output** and say so loudly when something is wrong. Then the KPI views get charts, so you can see what the pipeline produces.

## The idea in one picture
```
... four MERGEs (gold loaded) -> DataQualityChecks -> ReadDataQualityResult -> EvaluateDataQuality
                                  (one query, 14 checks)  (read the answer)       |
                                                                        "OK" -----+----> PipelineSucceeded
                                                                        other ----+----> DataQualityFailed -> PipelineFailed
                                                                                          (names the failing checks)
```

## What a check is
One SQL file in `sql/data_quality_checks/`. It returns **one row**: its own name and a number `failures`. **0 means pass.**
```sql
SELECT 'gold_fact_end_before_start' AS check_name, COUNT(*) AS failures
FROM pf_gold.fact_encounter WHERE end_ts < start_ts
```
Checks that look at one batch use `ingest_date` (a `?` parameter). The 14 files are combined into **one** Athena query (`UNION ALL`), so a pipeline run pays for one query and waits about one minute, not fourteen.

| Kind | Checks (file names) |
|---|---|
| Silver, for the batch | `silver_patient_has_rows`, `silver_encounter_has_rows`, `silver_patient_duplicate_keys`, `silver_encounter_duplicate_keys`, `silver_condition_duplicate_keys`, `silver_encounter_end_before_start` |
| Gold | `gold_dim_patient_duplicate_keys`, `gold_fact_duplicate_keys`, `gold_fact_end_before_start`, `gold_fact_without_patient`, `gold_fact_without_facility`, `gold_fact_without_practitioner`, `gold_fact_without_date`, `gold_batch_missing_in_fact` (every silver encounter of the batch reached the fact table) |

## What this phase added
| Item | Purpose |
|---|---|
| `sql/data_quality_checks/*.sql` (14 files) | The checks |
| `infra/shared/sql_files.py` | `read_checks`, `detailed_checks_query` (every check with its number), `failed_checks_query` (only the names of failing checks, or `OK`) |
| `step_functions/pf_batch_pipeline.asl.json` | Three new steps after the merges: `DataQualityChecks` (Athena), `ReadDataQualityResult`, `EvaluateDataQuality` (Choice) and a failure state `DataQualityFailed` that puts the names of the failing checks into the error |
| `infra/shared/pipeline_definitions.py` | Fills `__SQL_DQ__` in the workflow with the combined query, so the workflow always runs the SQL files of the same commit |
| `infra/notebook_support/aws_helpers.py` | `start_execution`, `wait_for_execution`, `execution_steps` |
| `notebooks/data_quality.ipynb` | Results of the 14 checks for the 4 batches, then **proves the checks can fail** |
| `notebooks/kpi_analysis.ipynb` | One chart per KPI view, each with its numbers in a small table |
| `tests/infra/shared/test_sql_files.py`, `tests/infra/shared/test_pipeline_definitions.py` | Shape of every check file (name = file name, `failures`, one `?`-parameter rule); the workflow order and the decision; the quality query holds every check file |

## Proof that the checks work (`data_quality.ipynb`)
| Test | Result |
|---|---|
| All 14 checks for batches 1-4 | Every cell is **0** |
| Plant one bad row in `fact_encounter` (ends before it starts; patient, facility and doctor do not exist) | Exactly four checks fail: `gold_fact_end_before_start`, `gold_fact_without_patient`, `gold_fact_without_facility`, `gold_fact_without_practitioner` |
| Run the **whole workflow** with the bad row planted | Status `FAILED`, error `DataQualityFailed`, cause `These data-quality checks failed: ...` (the four names). Every load ran first; the run stopped at the quality step |
| Remove the bad row (a `try ... finally` always does) | All 14 checks are 0 again |

## KPI charts (`kpi_analysis.ipynb`)
Five charts from the five views. One colour for one series, blue and orange for two (admissions vs discharges), a one-colour scale for the heatmap with **gray** for "no visits" so a zero is not mistaken for a small number. Each chart has a table beside it.
- Readmissions per decade: 0 + 8 + 5 + 4 = **17 readmissions of 114 inpatient stays** (14.9 %), the same numbers as the independent recount made when the gold layer was built.
- Length of stay is shown only for facilities with at least 2 stays. The first version showed one facility with a single 95-day stay, which squashed the other bars and said little.
- The data is synthetic and small (4 batches), so the charts show how the output looks, not hospital performance.

## Good to know
- The checks run **after** gold is written. A failed run has already loaded its data; it is marked failed so nobody trusts it. Fix the cause, release the batch again, and every step overwrites its own result (idempotent).
- To add a check: add a SQL file to `sql/data_quality_checks/` that returns `check_name` and `failures`. The next deployment puts it into the workflow.
- Not done (`[EXTRA]`): writing the check results to a table under `data_quality_results/` so trends over time can be charted.
