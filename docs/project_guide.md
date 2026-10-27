# Project guide: how everything fits together

For someone new to AWS data engineering. Read it top to bottom once; use the tables as a map afterwards.

---------------------------------------------------------------------------------------------------

## 1. The idea in one picture
Hospital visit records (fake, from Synthea, in the FHIR format) arrive as JSON files. A pipeline on AWS cleans them step by step and ends with tables that answer questions: *How long do patients stay? How many come back within 30 days?*

```
 YOUR LAPTOP                                    AWS  (region us-east-1, one S3 bucket "data lake")
 ----------                                     -----------------------------------------------------------

 Synthea  -->  341 patient files                landing/       BRONZE         SILVER            GOLD
   |           + hospital file                  (delivery      raw/original   clean tables      star schema
   v           + doctor file                     shelf)        copy           (Parquet)         (Iceberg)
 split into 10 batches (4 used)                    |              |              |                |
   |                                               |              |              |                |
   +--- aws s3 sync (upload once) -------------->  |              |              |                |
                                                   v              v              v                v
                                              Lambda          Glue job       Athena SQL       Athena views
                                              pf-ingest       pf-bronze-      MERGE            (5 KPIs)
                                              copies          to-silver       statements
                                              landing->       flattens        build gold
                                              bronze          JSON -> tables

 Step Functions runs these steps for one batch; a marker file landing/batch_NN/_SUCCESS starts it (EventBridge).   Power BI (Phase 13) will read the views.
```

---------------------------------------------------------------------------------------------------

## 2. The layers (medallion architecture)
| Layer | Where | What is in it | Why it exists |
|---|---|---|---|
| **landing** | `s3://<lake>/landing/batch_NN/` | The files exactly as delivered (4 batches, 139 files, 295 MB) | The delivery shelf; never modified |
| **bronze** | `bronze/patient_records/ingest_date=YYYY-MM-DD/` | The same files, copied by the Lambda, plus `_batch_metadata.json` (a receipt) | Keep the raw data untouched so any later step can be repeated |
| **silver** | `silver/<table>/ingest_date=.../` (Parquet) + Glue database `pf_silver` | 5 clean, typed tables | Easy to query; nested JSON is flattened; IDs and dates are normal |
| **gold** | `gold/<table>/` (Iceberg) + Glue database `pf_gold` | 4 dimensions, 1 fact, 5 views | The business model: ready for reports |

`ingest_date` is the simulated arrival day of a batch: batch 1 = 2026-11-01, batch 2 = 2026-11-02, and so on.

### Schemas (read from AWS)
**Silver** (`pf_silver`, partitioned by `ingest_date`):
| Table | Columns |
|---|---|
| `patient` | patient_id, gender, birth_date *(date)*, deceased_datetime *(timestamp)*, marital_status, city, state, postal_code |
| `encounter` | encounter_id, patient_id, status, encounter_class (AMB, EMER, IMP, HH, VR), type_code, type_display, start_ts, end_ts, organization_id, practitioner_npi, reason_code, reason_display |
| `condition` | condition_id, patient_id, encounter_id, code, display, clinical_status, onset_ts, abatement_ts |
| `organization` | organization_id, name, type, city, state, postal_code |
| `practitioner` | practitioner_id, npi, full_name, gender |

**Gold** (`pf_gold`, Iceberg): a **star schema**, one fact in the middle, dimensions around it.
```
                dim_patient                         dim_facility
                (patient_id)                        (organization_id)
                     \                                  /
                      \                                /
 dim_date  ------  fact_encounter  ------  dim_practitioner
 (date_key)        (encounter_id)           (npi)
```
| Table | Columns |
|---|---|
| `fact_encounter` (4,586 rows) | encounter_id, patient_id, organization_id, practitioner_npi, encounter_class, start_ts, end_ts, start_date, end_date, **los_hours, los_days** (length of stay), **is_inpatient, is_emergency**, reason_code, reason_display, primary_condition_code. Partitioned by year. |
| `dim_patient` (137) | patient_id, gender, birth_date, deceased_datetime, is_deceased, marital_status, city, state, postal_code, age_years, age_group |
| `dim_facility` (521) | organization_id, name, type, city, state, postal_code |
| `dim_practitioner` (521) | npi, practitioner_id, full_name, gender |
| `dim_date` (47,847 days, 1900–2030) | date_key, year, quarter, month, month_name, day_of_month, day_of_week, day_name, is_weekend |

**Views** (saved queries on the fact table): `vw_daily_admissions_discharges`, `vw_avg_los_by_facility_month`, `vw_readmission_30d`, `vw_ed_volume_by_hour_dow`, `vw_top_reasons`.

---------------------------------------------------------------------------------------------------

## 3. What each AWS service does here
| Service | Role in the project | Name |
|---|---|---|
| **S3** | Storage for every layer, and for Athena's result files | buckets `patientflow-datalake-…` and `patientflow-athena-query-results-…` |
| **Lambda** | A small function that copies one batch from `landing/` to `bronze/` | `pf-ingest` |
| **Glue job** | A Spark program that turns raw JSON into the 5 silver tables | `pf-bronze-to-silver` |
| **Glue Data Catalog** | The index of tables (names, columns, where the files are). Holds no data | databases `pf_silver`, `pf_gold` |
| **Athena** | Runs SQL on files in S3 (no server to manage); builds and queries gold | workgroup `pf-wg` (1 GB scan limit per query) |
| **IAM** | Who may do what: one role per service, each with only the permissions it needs | `pf-lambda-role`, `pf-glue-role`, `pf-step-functions-role`, `pf-trigger-role`, `pf-github-deploy-role` |
| **CloudWatch Logs** | The logs of the Lambda and Glue job | automatic |
| **Budgets** | Emails you at $5, $20, $50 | `pf-monthly-budget` |
| **EventBridge** | Watches for the marker file of a finished delivery and starts Step Functions | rule `pf-new-batch-rule` |
| **Step Functions** | Runs the steps in order for one batch: Lambda, Glue job, four merges; stops with an error if one fails | `pf-batch-pipeline` |

---------------------------------------------------------------------------------------------------

## 4. The data flow for one batch (what happens, in order)
| # | Step | Done by | Reads | Writes |
|---|---|---|---|---|
| 1 | Ingest | Lambda `pf-ingest` with `{"batch": N}` | `landing/batch_0N/` | `bronze/patient_records/ingest_date=<date>/` (+ receipt) |
| 2 | Flatten | Glue job with `--ingest_date <date>` | bronze files of that date | `silver/<5 tables>/ingest_date=<date>/` and the tables in `pf_silver` |
| 3 | Load gold | 4 Athena `MERGE` statements, dimensions first, then the fact | silver rows of that date | the 5 gold tables |
| 4 | Check quality | 14 SQL checks in one Athena query; a failing check ends the run as `FAILED` | gold and silver tables | the failing check names (in the error) |
| 5 | Report | Athena views | `fact_encounter` and dimensions | (nothing stored: a view is a query) |

**Step Functions** chains steps 1–3 into one workflow. It starts by itself when a sender writes the marker file `landing/batch_NN/_SUCCESS` (EventBridge), or by hand with `{"batch": N}`.
Every step is **idempotent**: running it twice for the same batch gives the same result (no duplicates).

---------------------------------------------------------------------------------------------------

## 5. Where the code runs
| Code | Runs on | Stored in the repo as | Deployed to AWS how |
|---|---|---|---|
| Lambda handler | AWS Lambda | `lambda/ingest/handler.py` | zip -> `update-function-code` (by `infra/deploy.py`) |
| Glue job | AWS Glue (Spark) | `glue_jobs/bronze_to_silver.py` + `fhir_utils.py` | copied to `s3://…/glue_scripts/<commit>/`, job updated (by `infra/deploy.py`) |
| SQL | Athena | `sql/gold/…` | read by a notebook (later by Step Functions) and sent to Athena |
| Notebooks | **your laptop** (they call AWS) | `notebooks/` | not deployed |
| Tests | your laptop and GitHub | `tests/` | not deployed |

---------------------------------------------------------------------------------------------------

## 6. CI/CD: what happens when you `git push`
```
git push (main) --> GitHub Actions: "Deploy" workflow
   job 1  checks    ruff + cfn-lint + pytest (204 tests, incl. Spark). Red = STOP, nothing is deployed.
   job 2  deploy    logs in to AWS with a short-lived identity (OIDC). No AWS keys are stored in GitHub.
                    infra/stacks.py:
                       - deploys the CloudFormation stack pf-foundation (buckets, catalog, workgroup, roles)
                       - uploads the code of this commit: glue_scripts/<commit id>/ and artifacts/<commit id>/
                       - deploys the stack pf-pipeline (Lambda, Glue job, workflow, trigger rule), which points at that code
Actions tab --> "Release batch" workflow (pick batch 1-4): writes the _SUCCESS marker and waits for the run
```
On a branch other than `main` or on a pull request, only the **CI** checks run (no deploy).
**CI** = Continuous Integration (check every change). **CD** = Continuous Deployment (ship it automatically).
*Pushed is not deployed* (Glue cannot read GitHub), and *deployed is not run* (running a batch is a separate action).

---------------------------------------------------------------------------------------------------

## 7. File map: what is each file and what was done in it

### Project root
| File | What it is |
|---|---|
| `PROJECT_PLAN.md` | The plan and change log: phases, design, decisions |
| `README.md` | GitHub front page (short; the full one comes in Phase 14) |
| `.gitignore` | Files Git must never store: secrets, the venv, data, the Synthea jar, build output |
| `pyproject.toml` | Settings of the lint tool (ruff) and the test tool (pytest) |
| `requirements.txt` | All Python packages for working locally (boto3, pandas, jupyter, pyspark, …) |
| `requirements-ci.txt` | The small, pinned list GitHub installs for the checks |

### `docs/` (one folder per phase, plus guides)
| File | Content |
|---|---|
| `phase_00 … phase_06 /README.md` | For each phase: goal, concepts, every command with why, results, problems and fixes |
| `phase_02_…/data_dictionary.md` | Which raw FHIR field fills which silver column |
| `cd_github_actions.md` | How the GitHub deployment and its AWS login work |
| `ci_testing_and_linting.md` | The tests, the lint, the CI workflow |
| `notebooks_and_aws.md` | What runs where (laptop vs AWS) |
| `project_guide.md` | This file |

### `.github/workflows/`
| File | What it does |
|---|---|
| `ci.yml` | Lint + tests. Runs on branches other than main, on pull requests, and when called by deploy.yml |
| `deploy.yml` | On a push to main: run ci.yml, then log in to AWS and run `infra/deploy.py` |

### `data_generation/`
| File | What it does |
|---|---|
| `split_batches.py` | Splits the Synthea output into 10 daily batches (hospital and doctor files go into batch 1). Only batches 1–4 are used. |

### `lambda/ingest/`
| File | What it does |
|---|---|
| `handler.py` | The Lambda: checks the batch number (1–4), copies the batch files to bronze, writes `_batch_metadata.json`. Fails on an empty batch. |

### `glue_jobs/`
| File | What it does |
|---|---|
| `fhir_utils.py` | The recipes: schemas of each FHIR resource, the reference resolver, the five table builders, checks, the table-DDL builder. No AWS inside, so it is tested on the laptop. |
| `bronze_to_silver.py` | The job: lists bronze files, builds and checks the tables, writes Parquet, registers the tables and partitions in the catalog |

### `sql/gold/`
| Folder | Files | What they do |
|---|---|---|
| `ddl/` | `dim_patient`, `dim_facility`, `dim_practitioner`, `fact_encounter`, `dim_date` | Create the gold tables (Iceberg) |
| `merge/` | `merge_dim_patient`, `merge_dim_facility`, `merge_dim_practitioner`, `merge_fact_encounter` | Upsert one batch into a gold table |
| `views/` | five `vw_…` files | The KPI views |

### `sql/dq/`
| Files | What they do |
|---|---|
| 14 files (`silver_…`, `gold_…`) | One data-quality check each: returns its name and the number of rows that break the rule (0 = pass). The workflow runs all of them as its last step |


### `step_functions/`
| File | What it does |
|---|---|
| `pf_batch_pipeline.asl.json` | The workflow: Ingest, BronzeToSilver, four merges, with retries and one failure state. `infra/deploy.py` inserts the merge SQL and deploys it |

### `infra/` (AWS setup and deploy helpers)
| File | What it does |
|---|---|
| `aws_helpers.py` | Shared helper used by the notebooks: `aws()` runs the AWS CLI (masks the account id), `run_athena`, `run_sql_file`, `run_glue_job`, `render_policy` |
| `sql_files.py` | Plain-Python SQL helpers (placeholders, splitting statements, parsing results); testable |
| `cloudformation/` | The infrastructure as YAML: `foundation.yaml`, `pipeline.yaml`, `github_oidc.yaml` (Phase 11-12) |
| `stacks.py` | Deploys the stacks: builds and uploads the code, runs `aws cloudformation deploy`. Used by GitHub Actions and the `cloudformation_stacks` notebook |
| `release_batch.py` | Delivers a batch (writes the marker) and waits for the run. Used by the Release batch workflow and `run_batches` |
| `teardown.py` | Deletes what the project created (retire the hand-built resources, or delete everything). Never runs by itself |
| `deploy.py` | Builds the settings, the Glue job definition, the Lambda package and the workflow definition (used by `stacks.py`); can also deploy them directly, as the first version of CD did |
| `s3/encryption.json`, `s3/lifecycle_athena_results.json` | Bucket settings: default encryption; delete query results after 7 days |
| `budget/budget.json` | The $50 monthly budget (the alert rules with your email are not in Git) |
| `policies/policy_*.json` | **Trust policies**: who may use a role (one per role) |
| `policies/permissions_*.template.json` | **Permission policies**: what a role may do. `{__PLACEHOLDERS__}` are filled from your settings |
| `policy_trigger.json`, `permissions_trigger.template.json` | The role that lets EventBridge start the workflow (and nothing else) |

### `config/`
| File | What it is |
|---|---|
| `settings.example.json` | Template of the project names. Your real `settings.json` (not in Git) holds your bucket names |

### `notebooks/` (each one builds or checks one thing, with its output saved)
| Notebook | What it does in AWS |
|---|---|
| `source_data_analysis` | Explores the raw FHIR data on your laptop; maps fields to silver columns; checks the joins |
| `s3_buckets` | Creates the two buckets (encryption, blocked public access, 7-day rule) |
| `create_catalog_databases` | Creates the Glue databases and the Athena workgroup |
| `iam_roles` | Creates the 4 service roles and tests 18 permissions |
| `upload_and_test` | Uploads the batches and runs the first Athena queries |
| `ingest_lambda` | Packages and deploys the Lambda, then tests it (ok, rerun, bad input) |
| `bronze_to_silver_job` | Deploys and runs the Glue job and checks silver against the source files |
| `github_deploy_role` | Creates the GitHub login trust and the deploy role; tests 14 permissions |
| `gold_layer` | Creates the gold tables, runs the merges, creates the views, checks everything against the source |
| `step_functions_pipeline` | Deploys the workflow and tests it: a normal run, a bad batch, two batches at once |
| `data_quality` | Results of the 14 checks for every batch; proves they can fail (plants a bad row, runs the workflow, removes it) |
| `kpi_analysis` | Five charts from the five KPI views |
| `run_batches` | Releases the 4 batches through the trigger, one at a time, and checks the gold tables, quality checks and KPI views |
| `cloudformation_stacks` | Checks the templates, retires the hand-built resources, deploys the 3 stacks, checks them (19 permission checks), reloads the data |
| `event_trigger` | Creates the folder trigger (bucket events, EventBridge rule) and tests it with real deliveries |

### `tests/` (204 tests, run by `pytest`, on your laptop and on GitHub)
| File | Tests |
|---|---|
| `test_ingest_handler.py` | The Lambda logic with a fake S3 |
| `test_fhir_utils.py` | The silver flattening with real local Spark |
| `test_sql_files.py` | The SQL helpers and every SQL file (rules like "partitioned by year") |
| `test_deploy.py` | The settings, job definition and Lambda package of the deployment |
| `test_json_files.py` | Every JSON file is valid |
| `test_cloudformation.py` | The templates: same permissions as the tested JSON policies, same settings as the code, least-privilege rules for the deploy roles |
| `test_release_batch.py` | Writing the marker and finding the run, with fake S3 and Step Functions |
| `test_teardown.py` | Deletion order, skipping what is gone, emptying a bucket first |
| `test_workflows.py` | The shape of the three GitHub workflows |

---------------------------------------------------------------------------------------------------

## 8. Which command to run for what
| I want to… | Do this |
|---|---|
| Check my code before committing | `python -m ruff check .` then `python -m pytest` |
| See who I am in AWS | `aws sts get-caller-identity` |
| Ingest a batch | invoke the Lambda with `{"batch": N}` (see the `ingest_lambda` notebook) |
| Transform a batch to silver | start the Glue job with `--ingest_date 2026-11-0N` (see `bronze_to_silver_job`) |
| Process a batch end to end | write `landing/batch_NN/_SUCCESS` containing `{"files": <count>}` after uploading the files (or start `pf-batch-pipeline` with `{"batch": N}`) |
| Load a batch into gold | `run_sql_file("sql/gold/merge/…", ingest_date=…)` (see `gold_layer`) |
| Deploy my code | `git push` to `main` (or `python infra/stacks.py --version <name> --role-arn <CloudFormation role>` by hand) |
| Load a batch | Actions tab > Release batch, or `notebooks/run_batches.ipynb` |
| Delete everything | `python infra/teardown.py --plan`, then `--delete-everything` |
| Ask a question about the data | Athena console → workgroup `pf-wg` → database `pf_gold` |
