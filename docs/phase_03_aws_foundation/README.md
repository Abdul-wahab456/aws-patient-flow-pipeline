# Phase 3 — AWS Foundation

## Goal
Create the base the pipeline stands on: storage (S3), a table catalog (Glue), a query engine with guardrails (Athena workgroup), and least-privilege permissions (IAM roles). Then upload the 10 batches and prove Athena works.

## Scope change (2026-10-26): 4 batches
To keep the project small while learning, only batches 1-4 are kept: 139 files, 294.9 MB. Batches 5-10 were deleted from S3 (`landing/batch_05` ... `batch_10`) and moved on the laptop to `data/landing_extra/`; `number_of_batches` in `config/settings.json` is now 4. The numbers in the sections below (343 files, 722.6 MB, 10 batches) describe the first upload. The `upload_and_test` notebook was re-run: S3 matches the 4 local batches.

## Bucket rename (2026-10-26)
The two buckets were renamed before Phase 5: `jared-patientflow-lake-zhmkbf` is now **`patientflow-datalake-zhmkbf`** and `jared-athena-results-zhmkbf` is now **`patientflow-athena-query-results-zhmkbf`**. S3 cannot rename a bucket, so the steps were: new names in `config/settings.json`; re-run `s3_buckets` (creates and secures the new buckets); server-side copy of `landing/` (`aws s3 sync s3://<old>/landing/ s3://<new>/landing/`: 139 objects and 294,910,903 bytes on both sides); re-run `create_catalog_databases` (the workgroup's results location is updated), `iam_roles` (policies point at the new bucket), `ingest_lambda` (new environment variable, batch 1 re-ingested) and `upload_and_test`. Nothing was lost: `bronze/` is rebuilt by the Lambda, and query results are disposable. The notebooks' saved outputs show the new names.

## Naming change (2026-10-26)
The S3 folders were renamed after this phase was built: `raw/fhir/` is now `bronze/patient_records/`, `scripts/` is `glue_scripts/`, `dq/` is `data_quality_results/`, and the unused `state/` folder was dropped. The IAM policies were updated and re-applied; the notebooks and results below use the new names where they refer to those folders.

## Plan changes since this phase (2026-10-23 / 2026-10-26)
The team asked for infrastructure as code, an event-driven trigger and CI/CD. Decision: build a **simple end-to-end pipeline first** (Phases 4-9, started by hand, deployed with CLI notebooks like this phase), then add the upgrades one at a time: event trigger (Phase 10), CloudFormation YAML (Phase 11), GitHub Actions CI/CD (Phase 12). Consequences for this phase: it stays as built (CLI notebooks); the resources are re-created from YAML in Phase 11, and `pf-scheduler-role` stays unused until Phase 10 replaces it with `pf-trigger-role`. See `PROJECT_PLAN.md`.

## What this phase creates
| # | Resource | Name | Purpose |
|---|---|---|---|
| 1 | S3 bucket (data lake) | `patientflow-datalake-<suffix>` | Holds every layer under prefixes: `landing/`, `bronze/patient_records/`, `silver/`, `gold/`, `glue_scripts/`, `data_quality_results/` |
| 2 | S3 bucket (query results) | `patientflow-athena-query-results-<suffix>` | Athena writes each query's result here; objects expire after 7 days |
| 3 | Glue databases | `pf_silver`, `pf_gold` | Catalog entries so Athena/Glue know our tables |
| 4 | Athena workgroup | `pf-wg` | Enforced results location + 1 GB scan limit per query |
| 5 | IAM roles | `pf-lambda-role`, `pf-glue-role`, `pf-step-functions-role`, `pf-scheduler-role` | One role per service, scoped to this project |
| 6 | Data upload | `s3://<lake>/landing/` | The 10 batches (722.6 MB) |

## Concepts
- **S3 bucket / prefix**: a bucket is a container with a *globally unique* name. A "folder" (prefix) is just part of an object's name; nothing needs to be created for it.
- **Block Public Access**: four switches that make it impossible to expose a bucket publicly by mistake.
- **Default encryption (SSE-S3)**: AWS encrypts every object at rest with keys it manages. No cost.
- **Lifecycle rule**: an automatic rule, e.g. "delete objects older than 7 days".
- **Naming**: `<suffix>` is a random 6-character string so the bucket names are globally unique. It is stored in `config/settings.json` (gitignored).

## Settings file
`config/settings.json` was created from `config/settings.example.json` with a random suffix (`config/settings.json` is gitignored). All scripts and commands read bucket names from it, so no name is typed twice. Region: `us-east-1`.

## How this phase is done: in a notebook
Phase 3 is split into **one notebook per component**, so each is short and easy to find:

| Notebook | Creates |
|---|---|
| `notebooks/s3_buckets.ipynb` | data lake bucket, Athena results bucket |
| `notebooks/create_catalog_databases.ipynb` | Glue databases, Athena workgroup |
| `notebooks/iam_roles.ipynb` | the four IAM roles |
| `notebooks/upload_and_test.ipynb` | upload of `data/landing`, Athena `SELECT 1` |

The next developer sees every step, the exact command and its output. The notebooks run the **real AWS CLI** through a small shared helper, `aws(...)`, in `infra/aws_helpers.py` (one copy, imported by all four notebooks). It prints each command as `$ aws ...` (copy-paste it into a terminal to run it by hand), and it forces the region from `config/settings.json`. Design rules:
- **Idempotent**: each creating cell first asks AWS whether the resource exists, so re-running is safe.
- **Settings from one file**: names and region come from `config/settings.json`.
- **Region forced** to the settings value through `AWS_DEFAULT_REGION`, so resources cannot land in the wrong region.
- **Verification cells** read the configuration back from AWS and `assert` it matches the design.
- The account id is never printed, so the notebook is safe to commit.

Why a notebook for infrastructure (decision): the team wants setup steps documented and runnable by the next developer. Trade-off we accepted: notebook cells can be run out of order, so the cells are written to be safe in any order and re-runnable.

## Why the Athena results bucket has a 7-day lifecycle rule
Every Athena query writes a result file into the results bucket, and notebooks and tests run many queries. Without a rule these files accumulate forever: slowly growing storage cost and clutter. The rule `infra/s3/lifecycle_athena_results.json` deletes each object 7 days after creation. If it were missing nothing would break; it is a cost guardrail from the plan.

## 3a — S3 buckets (`s3_buckets.ipynb`)
Note: the lake bucket already existed because it was created earlier from the terminal; the notebook detects this (`head-bucket`) and skips creation, which demonstrates the idempotent design.

Files used: `infra/s3/encryption.json` (default encryption rule: SSE-S3 / `AES256`), `infra/s3/lifecycle_athena_results.json` (expire results after 7 days).

| Step | Command | Why |
|---|---|---|
| Does it exist? | `s3api head-bucket` | decides whether to create (exit code 0 = exists) |
| Create | `s3api create-bucket --bucket <name> --region us-east-1` | In `us-east-1` no `LocationConstraint` is allowed; other regions require it |
| Block public access | `s3api put-public-access-block` (4 switches = true) | prevents accidental public exposure |
| Default encryption | `s3api put-bucket-encryption` with `file://infra/s3/encryption.json` | objects encrypted at rest with AWS-managed keys (free) |
| Lifecycle (results bucket) | `s3api put-bucket-lifecycle-configuration` | old Athena results deleted after 7 days |

**Results (notebook executed):**
- First build: the lake bucket (then `jared-patientflow-lake-zhmkbf`) already existed (created earlier from the terminal), so creation was skipped; public-access block and encryption applied.
- First build: the results bucket (then `jared-athena-results-zhmkbf`) was created, public-access block and encryption applied, 7-day expiry rule set.
- Verify cell: `OK` for both buckets (all four public-access switches on, encryption `AES256`) and `results expire after 7 days`.
- The AWS response to the lifecycle call contained `TransitionDefaultMinimumObjectSize: all_storage_classes_128K`. This is just AWS reporting a default; it only matters for storage-class transitions, which we do not use.
- Re-run test (done on copies, so saved outputs stayed intact): no errors, existing buckets reported as "already exists - skipping create", all `OK` lines again.

## 3b — Glue databases and Athena workgroup (`create_catalog_databases.ipynb`)

| Resource | Command | Why |
|---|---|---|
| Glue database `pf_silver` | `glue create-database --database-input '{"Name": ..., "Description": ...}'` | A Glue database is a named folder in the Glue Data Catalog. It stores table definitions, not data. The silver tables are registered here by the Glue job. |
| Glue database `pf_gold` | same | Gold Iceberg tables and the KPI views live here. |
| Athena workgroup `pf-wg` | `athena create-work-group --name pf-wg --configuration '{...}'` | A bundle of query settings, below. |

Workgroup settings (all in one JSON built in the notebook from `config/settings.json`):
| Setting | Value | Effect |
|---|---|---|
| `ResultConfiguration.OutputLocation` | `s3://<results bucket>/` | where every query result is saved |
| `EncryptionConfiguration` | `SSE_S3` | results are encrypted at rest |
| `EnforceWorkGroupConfiguration` | `true` | a query cannot override the location or limits |
| `BytesScannedCutoffPerQuery` | 1,073,741,824 (1 GB) | a query that would scan more is cancelled: protects the credits |
| `EngineVersion` | Athena engine version 3 | required for Iceberg `MERGE` |

How the notebook stays safe to re-run:
- Databases: `glue get-database` first; skipped if present.
- Workgroup: `athena get-work-group` first; if present, `update-work-group` re-applies the same settings, so it cannot drift.
- Before anything is created, the notebook asserts that the results bucket exists, and fails with a clear message if it does not.
- The verify cell reads the settings back and asserts them.

Notes:
- JSON is passed as a normal command argument from Python (`json.dumps(...)`); this avoids the PowerShell quoting problems of typing JSON in a terminal. Passing JSON this way was tested with a read-only validation call before use.
- Lake Formation permission mode is **not** enabled; access stays IAM-based (plan, Section 5).

**Results (notebook executed):**
- `pf_silver` and `pf_gold` created.
- Workgroup `pf-wg` created: state ENABLED, settings enforced, scan limit 1 GB per query, results to the results bucket, Athena engine version 3.
- Verify cell: `OK` for both databases and the workgroup.
- Re-run test (on copies): no errors; existing databases reported as "already exists - skipping", the workgroup settings were re-applied, all `OK` lines again.

## 3c — IAM roles (`iam_roles.ipynb`)

### Concepts
- An **IAM role** is an identity that an AWS *service* (not a person) uses. A service "assumes" the role and gets its permissions, with no access keys involved.
- **Trust policy** = *who* may assume the role (e.g. only `lambda.amazonaws.com`).
- **Permissions policy** = *what* the role may do (actions on resources).
- **Least privilege** = each role gets only what its job needs, on only this project's resources.
- **Managed policy** = a ready-made policy maintained by AWS, attached by ARN. **Inline policy** = our own policy stored inside the role.

### Files (`infra/policies/`)

Naming: `policy_<role>.json` is the **trust policy** (who may use the role) and `permissions_<role>.template.json` is the **permissions policy** (what the role may do). Both are needed for every role.

| File | Kind | Content |
|---|---|---|
| `policy_lambda.json`, `policy_glue.json`, `policy_step_functions.json`, `policy_scheduler.json` | trust policy: **who** | which service may assume the role |
| `permissions_lambda.template.json` | permissions | read `landing/`; write `bronze/`; list only those prefixes |
| `permissions_glue.template.json` | permissions | read `bronze/`, `glue_scripts/`; read, write, delete `silver/`, `temp/`; list the bucket |
| `permissions_step_functions.template.json` | permissions | invoke `pf-ingest`; run job `pf-bronze-to-silver`; run Athena in `pf-wg`; manage tables in `pf_silver` / `pf_gold`; read/write the lake and results buckets; workflow logging |
| `permissions_scheduler.template.json` | permissions | `states:StartExecution` on `pf-daily-pipeline` only |

`*.template.json` files contain placeholders (`__LAKE__`, `__ACCOUNT__`, `__REGION__`, ...) that the notebook fills in from `config/settings.json`, so no account id or bucket name is committed.

### Roles
| Role | Trusted service | AWS-managed policy attached | Own policy |
|---|---|---|---|
| `pf-lambda-role` | Lambda | `AWSLambdaBasicExecutionRole` (CloudWatch logs) | `permissions_lambda` |
| `pf-glue-role` | Glue | `AWSGlueServiceRole` (Glue service + logs) | `permissions_glue` |
| `pf-step-functions-role` | Step Functions | none | `permissions_step_functions` |
| `pf-scheduler-role` | EventBridge Scheduler | none | `permissions_scheduler` |

Every role is tagged `project=patient-flow`.

### Commands used (all printed by the notebook)
| Command | Why |
|---|---|
| `iam get-role` | does the role exist? |
| `iam create-role` / `iam update-assume-role-policy` | create it, or re-apply the trust policy if it exists |
| `iam tag-role` | tags (useful for cost and ownership tracking) |
| `iam attach-role-policy` | attach an AWS-managed policy (safe to repeat) |
| `iam put-role-policy` | set our inline permissions policy (safe to repeat) |
| `iam simulate-principal-policy` | ask IAM "would this role be allowed to do X?" without doing it |

### Verification: 18 permission checks (simulator)
The notebook tests what each role **must** be able to do and what it **must not** be able to do, and asserts the answers. Examples:
- Lambda role: may write `bronze/`, **may not** write `landing/` or `silver/`.
- Glue role: may write and delete in `silver/`, **may not** write `bronze/` or read `gold/`.
- Step Functions role: may use workgroup `pf-wg`, **may not** use the `primary` workgroup.
- Scheduler role: may start only `pf-daily-pipeline`, nothing else.

Results (notebook executed): all 4 roles created, trust policies confirmed, **all 18 checks passed**. Re-run test on a copy: no errors, roles reported as "already exists", checks passed again.

### Naming decision
`sfn` (short for AWS **S**tep **F**unction**s**) was spelled out: the role is `pf-step-functions-role` and the files are `policy_step_functions.json` / `permissions_step_functions.template.json`. IAM cannot rename a role, so the four unused roles were deleted (detach managed policies, delete inline policies, delete role) and the notebook created them again under the new names; the saved outputs show a clean creation of all four. `pf-` means *patient flow*.

### Design decisions and trade-offs
- **Glue role** can list the whole lake bucket (names only; reading is still limited to the prefixes above). Spark and Hadoop probe parent paths while reading and writing, and a listing restricted to specific prefixes can make a job fail in confusing ways. `AWSGlueServiceRole` is broad inside Glue by design.
- **Lambda role** is stricter: listing is limited to `landing/` and `bronze/`.
- **Step Functions role** needs wide S3 rights on the lake bucket because Athena (running with this role's rights) writes the gold Iceberg tables and their metadata. It is still limited to this project's two buckets.
- The roles are created **before** the Lambda, Glue job, state machine and schedule exist. A policy may name a resource that does not exist yet.
- `[EXTRA]` Possible later hardening: add a `aws:SourceAccount` condition to the trust policies. It guards against the "confused deputy" problem (another account's service being tricked into using our role) and is recommended in production. Not done here to keep the learning path simple.
- The notebook masks the account id in all output, and the verification found it in no committed file.

## 3d — Upload data and test Athena (`upload_and_test.ipynb`)

### Steps and commands
| Step | Command / function | Why |
|---|---|---|
| Check local batches | Python checks on `data/landing/` | Stop before uploading if a batch is missing, empty, or has a non-`.json` file, or if batch 1 lacks the hospital and practitioner files |
| Upload | `aws s3 sync data/landing s3://<lake>/landing/ --only-show-errors` | `sync` copies a folder and skips files already in S3 with the same size and date, so it can be repeated and only sends changes. `--only-show-errors` hides one line per file (hundreds). |
| Verify upload | `s3api list-objects-v2` compared with the local files | Same files and same sizes, batch by batch (`assert remote == local`) |
| Verify encryption | `s3api head-object` | Uploaded objects carry `AES256` (bucket default encryption works) |
| Test Athena | `run_athena("SELECT 1 AS test")` | Proves the workgroup, results bucket and permissions work end to end |
| Test catalog | `run_athena("SHOW DATABASES")` | Athena sees `pf_silver` and `pf_gold` |
| Test results location | `s3api list-objects-v2` on the results bucket | The query's result file was written where the workgroup enforces it |

`run_athena` (added to `infra/aws_helpers.py`) is reusable in Phases 6 and 8. Athena is asynchronous: `athena start-query-execution` only starts the query and returns an id; the function then polls `athena get-query-execution` until the state is `SUCCEEDED` (or fails with the reason if `FAILED`/`CANCELLED`, or times out), then reads the rows with `athena get-query-results`.

### Results (notebook executed)
- Local check: 10 batches, 343 files, 722.6 MB.
- Upload verified: 343 objects, 722.6 MB in `s3://<lake>/landing/`, identical to the local files in every batch (37, 34 x 9 files).
- Uploaded objects encrypted (`AES256`).
- `SELECT 1` succeeded, 0 bytes scanned.
- `SHOW DATABASES` returned `pf_gold` and `pf_silver`.
- The test query's result file (`.csv` and `.csv.metadata`) is in the results bucket.

## Phase 3 — done when
- `aws s3 ls`, `aws glue get-databases`, `aws athena get-work-group --work-group pf-wg`, `aws iam get-role` confirm the resources. ✅ (each notebook verifies its own resources by reading them back from AWS)
- Data uploaded and matching the laptop. ✅
- Athena `SELECT 1` succeeds. ✅ (this was postponed from Phase 0 because Athena needs the results bucket)
