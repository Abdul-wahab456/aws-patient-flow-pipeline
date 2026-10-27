# CD: deploying to AWS from GitHub (GitHub Actions)

> **History.** This note describes the first, hand-built version. The notebooks and JSON files it mentions were removed when the CloudFormation templates (`infra/cloudformation/`) replaced them; the design reasons and the test results still apply.

> **Update:** the deployment now deploys the CloudFormation stacks (`infra/github_actions/deploy_stacks.py`) with two roles. This page describes the first version, in which a script called `infra/deploy.py` (now `infra/shared/pipeline_definitions.py`, without the command-line part) deployed directly. The login idea, the OIDC trust and the versioned code below are unchanged; what the roles may do and the new steps are in [cicd_stacks.md](cicd_stacks.md). This page describes the first (direct) version, which taught the same login mechanism.

## The idea
Push code to `main` and AWS gets the new version by itself. Nobody copies files to S3 by hand, and **no AWS access key is stored in GitHub**.

```
git push (main)
   |
   v
GitHub Actions: deploy.yml
   1. checks   -> ci.yml: ruff + pytest. If this fails, nothing is deployed.
   2. deploy   -> logs in to AWS with a short-lived identity (OIDC), then runs infra/deploy.py
                    - uploads glue_jobs/*.py to s3://<lake>/glue_scripts/<commit id>/
                    - updates the Glue job so it reads exactly that commit's scripts
                    - replaces the code and settings of the ingest Lambda
```
**Pushed is not deployed:** AWS Glue cannot read GitHub. It reads the scripts from S3, so a deployment step has to copy them there. That step is what `deploy.yml` automates.
**Deploying is not running data:** a deployment changes the code only. Running a batch stays a separate, deliberate action.

## Files
| File | What it is |
|---|---|
| `.github/workflows/deploy.yml` | The workflow: runs on every push to `main` (and by hand from the Actions tab) |
| `.github/workflows/ci.yml` | The checks, called first by `deploy.yml`; on other branches and pull requests it runs by itself |
| `infra/deploy.py` | The deploy logic (also usable by hand, see below) |
| `iam_policies/policy_github_deploy.template.json` | Trust policy of the deploy role: who may use it |
| `iam_policies/permissions_github_deploy.template.json` | Permissions of the deploy role: what it may do |
| `github_deploy_role.ipynb` | Creates the OIDC provider and the role, and tests the permissions |
| `tests/infra/shared/test_pipeline_definitions.py` | 6 unit tests for the pure parts of `deploy.py` |

## How the login works (no stored keys)
1. When a workflow runs, GitHub can issue it a **signed token** that says who it is: for example "repository `Abdul-wahab456/aws-patient-flow-pipeline`, branch `main`".
2. AWS has an **OIDC identity provider** for GitHub, so it can check that signature.
3. The role `pf-github-deploy-role` has a **trust policy** that accepts the token only if its subject is exactly the value of `github_subject` in `config/settings.json`: `repo:<owner>@<owner id>/<repository>@<repository id>:ref:refs/heads/main`. A different repository, a fork, or another branch is refused. The numeric ids make the rule stronger than the name alone: a deleted repository's name can be taken by someone else, its id cannot.

   > **Lesson from the first real run:** the first version of the rule used the plain form `repo:<owner>/<repository>:ref:refs/heads/main`. GitHub's real token has the ids in it, so AWS answered `Not authorized to perform sts:AssumeRoleWithWebIdentity`. The exact subject GitHub sent was read from AWS's audit log (CloudTrail: `aws cloudtrail lookup-events --lookup-attributes AttributeKey=EventName,AttributeValue=AssumeRoleWithWebIdentity`, field `userIdentity.userName`) and the rule was corrected. The same command is the way to diagnose any future login failure. Audit-log entries can take a few minutes to appear.
4. The action `aws-actions/configure-aws-credentials` exchanges the token for **temporary** AWS credentials (about an hour). Nothing permanent exists that could leak.

The job needs `permissions: id-token: write` to ask GitHub for the token. The CI job has `contents: read` only.

## What the deploy role may do (least privilege)
| May | May not (tested with the policy simulator) |
|---|---|
| Upload files under `glue_scripts/` | Write `bronze/`, `silver/`, `gold/`, read `landing/`, delete anything |
| Read/create/update the one Glue job `pf-bronze-to-silver` | Touch any other Glue job |
| Update the one Lambda `pf-ingest` (code and configuration) | Touch any other Lambda |
| Hand `pf-glue-role` to Glue and `pf-lambda-role` to Lambda (`iam:PassRole`, only for those services) | Create roles, hand over any other role |

14 simulator checks pass (5 allowed, 9 denied).

## Versioned scripts and rollback
Each deployment uploads to its own folder named by the Git commit id: `glue_scripts/<commit id>/bronze_to_silver.py` and `fhir_utils.py`. The Glue job is then pointed at that exact folder. Consequences:
- Every job run can be traced to the commit that produced it (the job's script path shows the commit).
- **Rollback** = deploy an older commit (re-run an old workflow run, or `python infra/deploy.py --version <older commit>` after checking it out).
- Old folders stay (a few kilobytes each). They can be cleaned later.

## One-time setup
1. **Create the AWS side** by running `github_deploy_role.ipynb` (done).
2. **Push the repository** to GitHub.
3. **Add repository variables** (Settings > Secrets and variables > Actions > **Variables**). They are names, not secrets:

   | Variable | Value |
   |---|---|
   | `AWS_DEPLOY_ROLE_ARN` | output of `aws iam get-role --role-name pf-github-deploy-role --query Role.Arn --output text` |
   | `AWS_REGION` | `us-east-1` |
   | `LAKE_BUCKET` | the data lake bucket name (`config/settings.json`) |
   | `SILVER_DATABASE` | `pf_silver` |

4. Push a change to `main` (or press **Run workflow** on the Deploy workflow) and read the result in the **Actions** tab.

## Deploy by hand (same code, from the laptop)
```powershell
python infra/deploy.py --version manual-test
```
Settings are read from `config/settings.json`; the laptop's own AWS login is used. Useful to test before relying on GitHub.

## Good to know
- **Whoever deployed last wins.** The notebooks `bronze_to_silver_job` and `ingest_lambda` also deploy (the manual way, with unversioned script paths). If you run them after a GitHub deployment, the job points back to the unversioned scripts until the next push. Normal work: change code, push, let GitHub deploy.
- A failed check (lint or test) stops the workflow before any AWS call.
- Two pushes close together do not deploy at the same time (`concurrency: deploy`).
- This is the first version of CD: it uses boto3 calls. Later the same workflow deploys the CloudFormation stacks instead; the login, the versioning idea and the checks stay.

## Verified end to end (2026-10-26)
| Check | Result |
|---|---|
| Push of commit `b1c03e2` to `main` | `Deploy` workflow green: `checks` job (lint + 58 tests) then `deploy` job |
| S3 | new folder `glue_scripts/b1c03e262a.../` holding `bronze_to_silver.py` and `fhir_utils.py` |
| Glue job | `ScriptLocation` and `--extra-py-files` point at that commit's folder; settings unchanged (Glue 5.0, 2 workers, 10 min, 0 retries) |
| Lambda | code and configuration updated by the same deployment |
| The deployed Lambda, batch 1 | `OK`, 37 files |
| The deployed Glue job, batch 1 | `SUCCEEDED` in 110 s (about $0.03); patient 35, encounter 1,411, condition 854, organization 521, practitioner 521: identical to the earlier verified run |

Problems met on the way (both fixed): the variable `AWS_DEPLOY_ROLE_ARN` first held an instruction sentence instead of the ARN (error "Source Account ID is needed if the Role Name is provided and not the Role ARN"), and the trust rule expected GitHub's subject without the numeric ids (error "Not authorized to perform sts:AssumeRoleWithWebIdentity").
