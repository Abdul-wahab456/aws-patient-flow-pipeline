# Infrastructure as CloudFormation

## Goal
Until now every AWS resource was created by a command in a notebook. If the account were wiped, rebuilding would mean running ten notebooks in the right order and remembering the settings. Now the same resources are described in **three YAML files**; one command (or one `git push`) creates or updates all of them, and one command deletes them again.

## The words
| Word | Plain meaning |
|---|---|
| **Template** | A YAML file that says *what should exist* (a bucket, a role, a job), not *how to make it* |
| **Stack** | One deployed template. AWS remembers which resources belong to it |
| **Change set** | The list of changes AWS would make if you deployed the changed template. `aws cloudformation deploy` builds one and runs it. Nothing changed = nothing happens |
| **Parameter** | A value given when deploying (the bucket names, the prefix, the code version) so the template has no secrets or names written in |
| **Export / ImportValue** | How one stack hands a value to another (the pipeline stack reads the role ARNs and the bucket name from the foundation stack). AWS then refuses to delete the foundation while the pipeline still uses it |
| **Service role** | The identity CloudFormation uses to create things. It is allowed only what the stacks need |

## The three stacks
| Stack | File | Holds |
|---|---|---|
| `pf-cicd` | `infra/cloudformation/github_oidc.yaml` | The **deploy role** that GitHub logs in as, and the **CloudFormation role** (details in `cicd_stacks.md`) |
| `pf-foundation` | `infra/cloudformation/foundation.yaml` | Lake bucket and results bucket, the catalog databases `pf_silver`/`pf_gold`, the workgroup `pf-wg`, the four service roles |
| `pf-pipeline` | `infra/cloudformation/pipeline.yaml` | Ingest Lambda, Glue job, Step Functions workflow, the EventBridge rule |

Order: `pf-cicd` (by hand, once), then `pf-foundation`, then the code is uploaded, then `pf-pipeline`. The code is **not** inside the template. `infra/github_actions/deploy_stacks.py` uploads it to the lake bucket under the version (the Git commit id): the Glue scripts to `glue_scripts/<version>/`, and the Lambda zip and the finished workflow definition to `artifacts/<version>/`. The pipeline stack points at those keys, so a deployment always names the exact code it runs.

## Decisions worth knowing
* **The buckets are `DeletionPolicy: Retain`.** Deleting a stack never deletes data. `infra/operations/teardown.py` empties and deletes the buckets deliberately (see `teardown.md`).
* **The permissions exist twice** — as JSON files (`iam_policies/`, used by the learning notebooks and tested with the policy simulator) and inside `foundation.yaml`. `tests/infra/cloudformation/test_templates.py` loads both and fails if they differ, so they cannot drift apart.
* **The workflow definition is built, not written twice.** `step_functions/pf_batch_pipeline.asl.json` plus the SQL files become `state_machine.json`, exactly as before (`deploy.state_machine_definition`); the stack reads that file from S3 (`DefinitionS3Location`).
* **Names come from a prefix** (`pf` → `pf-lambda-role`, `pf-wg`, …). The catalog databases stay `pf_silver` and `pf_gold` because the SQL files name them.
* **Log groups are resources too** (30 days of retention) so deleting the stack deletes them.

## Tests before AWS
`tests/infra/cloudformation/test_templates.py` (26 tests): the four service roles equal the tested JSON policies; role names; buckets private, encrypted, retained; EventBridge on the lake bucket; results expire after 7 days; the workgroup enforces its settings and the 1 GB scan limit; the Lambda and the Glue job have the same settings as `pipeline_definitions.py` gives them; the template reads the code from the keys `deploy_stacks.py` uploads; the workflow built for another prefix uses that prefix's names; the event pattern is the one tested in the event trigger; the `deploy` command has the stack name, capabilities and parameters; every parameter without a default is supplied. `cfn-lint` runs on all three files (and in CI).

## Tests on AWS
**A trial in a second region first.** The real resources have the real names, so deploying the same templates there would fail. Instead the three stacks were deployed with the prefix `pft` in **us-west-2** (new bucket names; the catalog databases are per region, so `pf_gold` could exist there too). Result: `pft-cicd`, `pft-foundation` and `pft-pipeline` reached `CREATE_COMPLETE` on the first try, **created by the CloudFormation role** (so its permission list was proven by doing, not by reading), and one batch ran from the marker to the end through the new resources. The trial stacks and buckets were deleted afterwards with `infra/operations/teardown.py` (no `pft` resource is left).

**The real deployment** is `notebooks/cloudformation_stacks.ipynb`: check the templates (cfn-lint and `validate-template`), retire the hand-built resources, deploy the three stacks, list what each stack holds, check every resource against the settings of the first notebook build, 19 permission checks with the policy simulator, upload the batches and create the gold tables.

## Retiring the hand-built resources
CloudFormation cannot create a name that already exists, so the the first notebook build resources are deleted first (`infra/operations/teardown.py`, `hand_built_resources`): trigger rule, workflow, Glue job, Lambda, workgroup, catalog databases, the five roles and **both buckets with all their data**. The data is rebuilt: `landing/` is uploaded again from `data/landing/` and `run_batches.ipynb` loads the four batches (about 40 minutes, about $0.12).
The deleting cell refuses to run unless it is told to (`PF_CONFIRM_RETIRE=yes`) **and** it refuses when the foundation stack exists, so running the notebook again can never delete what CloudFormation owns.

## What changed for the earlier notebooks
They stay in the repository as the step-by-step manual build (the best way to see what each service does). They must not be run again against the stacks: they would change resources behind CloudFormation's back.

## Good to know
* **Drift**: a change made outside CloudFormation (console, CLI) is not in the template; the next deployment may undo it. Change the YAML instead.
* **Rollback**: if a stack create fails, CloudFormation deletes what it made; for an update it restores the previous state. The reason is in the stack's *Events* tab.
* **Cost**: CloudFormation itself is free for these resource types.

## The real swap (done)
`cloudformation_stacks.ipynb` ran with deletion enabled and **succeeded on the first run**: 16 hand-built resources retired (4,389 objects in the lake bucket and 1,047 in the results bucket removed with them), `pf-cicd`, `pf-foundation` and `pf-pipeline` all `CREATE_COMPLETE`, every resource check and all 19 permission checks passed, the four batches re-uploaded and the gold tables and views recreated. `run_batches.ipynb` then loaded the four batches again through the trigger on the new resources (the batch run numbers repeated).
