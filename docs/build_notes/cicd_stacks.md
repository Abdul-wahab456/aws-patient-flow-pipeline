# CI/CD deploys the CloudFormation stacks

## Goal
A change pushed to `main` is checked and deployed with no manual command, and loading a batch is one click. No AWS key is stored in GitHub.

## What happens on `git push`
```
push to a branch / pull request ──► ci.yml:  ruff, cfn-lint, pytest (incl. Spark)        (no AWS access at all)

push to main ──► deploy.yml
                   job 1  checks   = ci.yml. Red = STOP, nothing is deployed
                   job 2  deploy   GitHub gives the job a short-lived identity token
                                   AWS checks it against the OIDC trust of pf-github-deploy-role
                                   python -m infra.github_actions.deploy_stacks --version <commit id> --role-arn <CloudFormation role>
                                     1. deploy stack pf-foundation   (buckets, catalog, workgroup, roles)
                                     2. upload the code of this commit: glue_scripts/<commit>/, artifacts/<commit>/
                                     3. deploy stack pf-pipeline     (Lambda, Glue job, workflow, trigger rule)
Actions tab ──► release-batch.yml (click, type a batch number 1-99): writes landing/batch_NN/_SUCCESS and waits for the run
```
Pushed ≠ deployed (deployment only starts if the checks are green). Deployed ≠ run: **a deployment never loads data**. Loading is the separate `Release batch` click, because re-running data is a deliberate act.

## Two roles, on purpose
| Role | Who uses it | May do |
|---|---|---|
| `pf-github-deploy-role` | GitHub Actions (only this repository's `main` branch; the token's subject contains the numeric repository ids) | start, update and read the two pipeline stacks; upload code to `glue_scripts/` and `artifacts/`; write `landing/*/_SUCCESS`; read the workflow's runs; **pass the CloudFormation role to CloudFormation, nothing else** |
| `pf-cloudformation-role` | CloudFormation itself | create and update the pipeline's resources, **only those names**: four IAM roles (and attach only two AWS-managed policies), the two buckets, two catalog databases, the Glue job, the workgroup, the Lambda, the workflow, the rule, their log groups |

Why two: if GitHub's role could create resources itself it would also be able to create an administrator role — a stolen GitHub token would be a stolen account. Now the login role can only *ask CloudFormation* to deploy templates from this repository, and CloudFormation's role can only touch resources with the project's names. Both are defined in `infra/cloudformation/github_oidc.yaml` (stack `pf-cicd`), which an administrator deploys once.

Checked in the notebook with the policy simulator: the deploy role may `UpdateStack` on `pf-pipeline` but not `DeleteStack`, may upload to `artifacts/` but not write `gold/`, may not create a Lambda or an IAM role, and may pass the CloudFormation role only to `cloudformation.amazonaws.com`; the CloudFormation role may create the Lambda `pf-ingest` but not `somebody-else`, may create the role `pf-glue-role` but not `admin-backdoor`, and may not attach arbitrary policies.

## What changed in the files
| File | Change |
|---|---|
| `.github/workflows/ci.yml` | New step: `cfn-lint infra/cloudformation/*.yaml` |
| `.github/workflows/deploy.yml` | Runs `infra/github_actions/deploy_stacks.py` instead of `infra/shared/pipeline_definitions.py` |
| `.github/workflows/release-batch.yml` | New. `workflow_dispatch` with a number box (any batch 1-99, no fixed list); one release at a time (`concurrency`) |
| `infra/github_actions/release_batch.py` | Writes the marker (the file count is counted at that moment), finds the one run EventBridge starts, waits, exits with an error unless it SUCCEEDED |
| `infra/github_actions/deploy_stacks.py` | Builds and uploads the artifacts, runs `aws cloudformation deploy` |
| `tests/infra/github_actions/test_workflows.py`, `tests/infra/github_actions/test_release_batch.py` | The shape of the three workflows; the release logic with fake S3 and Step Functions clients |

## Repository variables (Settings → Secrets and variables → Actions → Variables)
Names, not secrets. Existing: `AWS_DEPLOY_ROLE_ARN` (same role name as before, so the value stays), `AWS_REGION`, `LAKE_BUCKET`, `SILVER_DATABASE`. (`NUMBER_OF_BATCHES` is no longer used: delete it if it is still set.) **New:** `RESULTS_BUCKET` (the Athena results bucket) and `AWS_CLOUDFORMATION_ROLE_ARN` (the `CloudFormationRoleArn` output of the `pf-cicd` stack; the notebook prints it).

## Good to know
* **Roll back** by deploying an older commit (re-run the Deploy workflow on it): the stacks point back at that commit's code.
* **A failed stack update** is rolled back by CloudFormation; the reason is in the Events tab of the stack and in the Actions log.
* `infra/shared/pipeline_definitions.py` (direct deployment of the Glue job, the Lambda and the workflow) is not used by CD any more. The Step Functions and event-trigger notebooks still call it; do not run them against the stacks.

## Status: verified
* The Deploy workflow ran green on GitHub (login with the OIDC role, both stacks deployed as the CloudFormation role).
* **Release batch 5** from the Actions tab (the number box, no code change for the new batch): the Lambda accepted batch 5, the pipeline ran for ingest date 2026-11-05 and SUCCEEDED (34 files, 7 minutes). Silver holds 34 patients, 968 encounters and 683 conditions for that date; gold grew from 137 to 171 patients and from 4,586 to 5,554 encounters, all 968 new encounters reached the fact table, and the 14 data-quality checks pass for the new date. Organizations and practitioners are delivered only with batch 1, so those two dimensions stay at 521.
