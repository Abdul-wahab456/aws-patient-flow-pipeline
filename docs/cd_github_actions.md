# CD: deploying to AWS from GitHub (GitHub Actions)

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
| `infra/policies/policy_github_deploy.template.json` | Trust policy of the deploy role: who may use it |
| `infra/policies/permissions_github_deploy.template.json` | Permissions of the deploy role: what it may do |
| `notebooks/github_deploy_role.ipynb` | Creates the OIDC provider and the role, and tests the permissions |
| `tests/test_deploy.py` | 6 unit tests for the pure parts of `deploy.py` |

## How the login works (no stored keys)
1. When a workflow runs, GitHub can issue it a **signed token** that says who it is: for example "repository `Abdul-wahab456/aws-patient-flow-pipeline`, branch `main`".
2. AWS has an **OIDC identity provider** for GitHub, so it can check that signature.
3. The role `pf-github-deploy-role` has a **trust policy** that accepts the token only if its subject is exactly `repo:<owner>/<repository>:ref:refs/heads/main`. A different repository, a fork, or another branch is refused.
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
1. **Create the AWS side** by running `notebooks/github_deploy_role.ipynb` (done).
2. **Push the repository** to GitHub.
3. **Add repository variables** (Settings > Secrets and variables > Actions > **Variables**). They are names, not secrets:

   | Variable | Value |
   |---|---|
   | `AWS_DEPLOY_ROLE_ARN` | output of `aws iam get-role --role-name pf-github-deploy-role --query Role.Arn --output text` |
   | `AWS_REGION` | `us-east-1` |
   | `LAKE_BUCKET` | the data lake bucket name (`config/settings.json`) |
   | `SILVER_DATABASE` | `pf_silver` |
   | `NUMBER_OF_BATCHES` | `4` |

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
- This is the first version of CD: it uses boto3 calls. In Phase 11/12 the same workflow will deploy the CloudFormation stacks instead; the login, the versioning idea and the checks stay.
