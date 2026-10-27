# Phase 12 — CI/CD deploys the CloudFormation stacks

## Goal
A change pushed to `main` is checked and deployed with no manual command, and loading a batch is one click. No AWS key is stored in GitHub.

## What happens on `git push`
```
push to a branch / pull request ──► ci.yml:  ruff, cfn-lint, pytest (incl. Spark)        (no AWS access at all)

push to main ──► deploy.yml
                   job 1  checks   = ci.yml. Red = STOP, nothing is deployed
                   job 2  deploy   GitHub gives the job a short-lived identity token
                                   AWS checks it against the OIDC trust of pf-github-deploy-role
                                   python infra/stacks.py --version <commit id> --role-arn <CloudFormation role>
                                     1. deploy stack pf-foundation   (buckets, catalog, workgroup, roles)
                                     2. upload the code of this commit: glue_scripts/<commit>/, artifacts/<commit>/
                                     3. deploy stack pf-pipeline     (Lambda, Glue job, workflow, trigger rule)
Actions tab ──► release-batch.yml (click, choose batch 1-4): writes landing/batch_NN/_SUCCESS and waits for the run
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
| `.github/workflows/deploy.yml` | Runs `infra/stacks.py` instead of `infra/deploy.py` |
| `.github/workflows/release-batch.yml` | New. `workflow_dispatch` with a choice of batch 1-4; one release at a time (`concurrency`) |
| `infra/release_batch.py` | Writes the marker (the file count is counted at that moment), finds the one run EventBridge starts, waits, exits with an error unless it SUCCEEDED |
| `infra/stacks.py` | Builds and uploads the artifacts, runs `aws cloudformation deploy` |
| `tests/test_workflows.py`, `tests/test_release_batch.py` | The shape of the three workflows; the release logic with fake S3 and Step Functions clients |

## Repository variables (Settings → Secrets and variables → Actions → Variables)
Names, not secrets. Existing: `AWS_DEPLOY_ROLE_ARN` (same role name as before, so the value stays), `AWS_REGION`, `LAKE_BUCKET`, `SILVER_DATABASE`, `NUMBER_OF_BATCHES`. **New:** `RESULTS_BUCKET` (the Athena results bucket) and `AWS_CLOUDFORMATION_ROLE_ARN` (the `CloudFormationRoleArn` output of the `pf-cicd` stack; the notebook prints it).

## Good to know
* **Roll back** by deploying an older commit (re-run the Deploy workflow on it): the stacks point back at that commit's code.
* **A failed stack update** is rolled back by CloudFormation; the reason is in the Events tab of the stack and in the Actions log.
* `infra/deploy.py` (direct deployment of the Glue job, the Lambda and the workflow) is not used by CD any more. The Phase 7-10 notebooks still call it; do not run them against the stacks.

## Status
Built and checked: the workflow files parse and have the expected shape (tests), the roles exist in AWS (stack `pf-cicd`) and passed the policy-simulator checks, and `infra/stacks.py` deployed the real stacks (as the CloudFormation role) from the notebook. **Not yet seen:** the first run of `deploy.yml` and `release-batch.yml` on GitHub itself. After the two new repository variables are set and the commit is pushed, check the Actions tab; if a step fails, the log says which permission was missing and the fix is one line in `github_oidc.yaml`.
