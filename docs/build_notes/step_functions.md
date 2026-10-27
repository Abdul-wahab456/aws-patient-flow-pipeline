# Orchestration with Step Functions

> **History.** This note describes the first, hand-built version. The notebooks and JSON files it mentions were removed when the CloudFormation templates (`infra/cloudformation/`) replaced them; the design reasons and the test results still apply.

## Goal
Run the whole pipeline for one batch with **one command**: ingest the files, build silver, load gold, one step after another, stopping with a clear error if anything fails. Before this phase, you ran each step by hand from a notebook.

```
start-execution {"batch": N}
   |
   v
 Ingest (Lambda) -> BronzeToSilver (Glue job) -> PrepareDateParameter -> MergeDimPatient -> MergeDimFacility
        -> MergeDimPractitioner -> MergeFactEncounter -> PipelineSucceeded
   any step fails (after its retries) -> PipelineFailed (keeps the original error and cause)
```

## What this phase creates
| Item | Purpose |
|---|---|
| `step_functions/pf_batch_pipeline.asl.json` | The workflow definition (ASL, Amazon States Language) as a template |
| `infra/shared/pipeline_definitions.py` additions | `state_machine_definition()` fills the template, `deploy_state_machine()` creates or updates the workflow; `main()` deploys it with the Glue job and the Lambda |
| `tests/infra/shared/test_pipeline_definitions.py` additions | 9 tests on the workflow |
| `iam_policies/permissions_github_deploy.template.json` | The GitHub deploy role may now also manage the one workflow |
| AWS: state machine `pf-batch-pipeline` | Standard workflow, role `pf-step-functions-role` |
| AWS: log group `/aws/vendedlogs/states/pf-batch-pipeline` | Errors only, kept 30 days |
| `step_functions_pipeline.ipynb` | Deploys the workflow and tests it on AWS |

## Concepts
- **Step Functions**: AWS's workflow service. You describe the steps and what happens on errors; it runs them, remembers where it is, and keeps a history of every step.
- **State machine**: one workflow definition. An **execution** is one run of it.
- **State**: one step. Types used here: `Task` (does work), `Pass` (reshapes data), `Succeed`, `Fail`.
- **Standard workflow**: can run for up to a year and keeps a full history. Fine for a pipeline that takes minutes.
- **`.sync` integration**: the step *waits* until the called service (Glue job, Athena query) has finished. Without it, the step ends as soon as the job is started.
- **Retry / Catch**: `Retry` repeats a step on named errors (with growing waits); `Catch` sends any remaining error to another step.
- **Execution input**: the JSON you pass when you start it; here `{"batch": 3}`. Each step adds its result to this JSON (`$.ingest`, `$.glue`, ...), so later steps can read earlier results.

## The steps
| Step | Does | Uses | If it fails |
|---|---|---|---|
| `Ingest` | invokes `pf-ingest` with the input; keeps `batch`, `ingest_date`, `file_count` | Lambda | retried 3 times on temporary Lambda errors; anything else, or an error raised by the function (such as "batch must be between 1 and 4"), goes to `PipelineFailed` |
| `BronzeToSilver` | starts the Glue job with `--ingest_date`, waits for it to finish | Glue (`.sync`) | **If the job is busy** (it allows one run at a time) the step waits 30 s, then 45 s, … up to 8 tries instead of failing |
| `PrepareDateParameter` | builds the text `'2026-11-01'` (with quotes) | `Pass` | – |
| `MergeDimPatient`, `MergeDimFacility`, `MergeDimPractitioner` | run the three dimension merges, one after the other | Athena (`.sync`) in workgroup `pf-wg` | Athena throttling retried; a failed query retried twice |
| `MergeFactEncounter` | runs the fact merge (it needs the dimensions first). It has two `?`, so it receives the date twice | Athena (`.sync`) | same |
| `PipelineSucceeded` / `PipelineFailed` | end states. Success returns `{batch, ingest_date, file_count}`; failure keeps the original error and cause | – | – |

How the SQL gets in: the template contains markers like `"__SQL:merge_dim_patient__"`. At deploy time `infra/shared/pipeline_definitions.py` replaces each marker with the text of `sql/gold/merge_batch/merge_dim_patient.sql`. The SQL therefore exists in one place only, and the workflow always runs the SQL files of the same commit.
The date reaches the SQL through Athena *execution parameters*: the `?` in the SQL is filled with `States.Array($.sql.date)`, where `$.sql.date` holds `'2026-11-01'`.

## Tests before AWS (`tests/infra/shared/test_pipeline_definitions.py`, 9 new tests)
Definition has no unfilled placeholders and names the right Lambda, Glue job and workgroup; the steps run in the expected order (dimensions before the fact); every `Next` points to a real state and no step leads nowhere; **every task catches errors and sends them to `PipelineFailed`**; **each merge gets exactly as many date parameters as it has `?`**; the merge steps carry the SQL of their files; the Glue step waits for a busy job.
Also checked by AWS itself (`validate-state-machine-definition`: OK) and `test-state` for the quoted-date expression (`'2026-11-01'`).

## Tests on AWS (`step_functions_pipeline.ipynb`)
| Test | Result |
|---|---|
| Deploy | state machine `ACTIVE`, type `STANDARD`, logging level `ERROR` |
| **Batch 1 end to end** | `SUCCEEDED`, output `{"file_count": 37, "batch": 1, "ingest_date": "2026-11-01"}`; gold row counts unchanged (137 / 521 / 521 / 4,586): the pipeline is repeatable |
| **Bad batch (5)** | `FAILED` at the first step with `ValueError: batch must be between 1 and 4, got 5`; the Glue job and the merges did not run |
| **Batches 3 and 4 started at the same moment** | both `SUCCEEDED`. Batch 4 waited at the Glue step (271 s instead of 124 s): the workflow retried the "job is busy" answer 3 times, as designed |
| Final state | no duplicate keys, same row counts |

Step times of batch 1: Ingest 10 s, BronzeToSilver 174 s, the four merges 61 + 62 + 58 + 56 s. About 7 minutes in total.

## Observation: most of the merge time is waiting
The merge queries themselves take **1–7 seconds** (from Athena's own records), but each merge *step* takes about **60 seconds**: the `.sync` Athena integration checks for completion about once a minute. So roughly 4 minutes of every run are waiting.
Possible improvements `[EXTRA]`, not done:
1. Run the three dimension merges **in parallel** (a `Parallel` state): saves about 2 minutes, because they write to different tables.
2. Replace `.sync` with a short loop (start the query, `Wait` 5 s, check, repeat): each merge would take about 10 s.
For a pipeline that processes one batch per day, 7 minutes is acceptable, so the simple version stays for now.

## How to run it
- Console: **Step Functions → State machines → `pf-batch-pipeline` → Start execution**, input `{"batch": 2}`. You watch the steps turn green in the picture.
- CLI: `aws stepfunctions start-execution --state-machine-arn <arn> --input '{"batch": 2}'`
- Notebook: `run_pipeline(2)` in `step_functions_pipeline.ipynb`.
Look at the result: **Executions** tab → open one → the colored graph, with the input and output of every step.

## Deployment by GitHub
`infra/shared/pipeline_definitions.py` now also creates/updates the workflow, so a push to `main` redeploys it with the Glue job and the Lambda. The deploy role was given exactly the new permissions needed (manage the one workflow; hand `pf-step-functions-role` to Step Functions; create its log group; set up workflow logging) and nothing else: 21 simulator checks pass (it still cannot delete the workflow, touch another workflow, or **start executions**).
A new Lambda or Glue job version is not "run" by a deployment: starting a batch stays a separate action.

## Concurrency, in plain words
- Two batches at once: the second waits at the Glue step (works, tested).
- The merges of two batches could write the same Iceberg table at the same moment; the merge steps retry once or twice if that conflict happens. It did not occur in the test.

## Done when
- One execution runs the full flow end to end. ✅
- A rerun of the same batch is idempotent. ✅
- A failing step fails the execution with the original error. ✅ (the data-quality step was added later)
