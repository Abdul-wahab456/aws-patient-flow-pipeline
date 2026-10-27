# Access Checks

## Goal
Confirm that this laptop can talk to AWS safely, and that every service the project needs is available on the Free plan, **before** building anything.

## Concepts
- **AWS CLI**: a terminal program that sends API requests to AWS. Every command has the shape `aws <service> <operation> [--options]`.
- **IAM user (`jared-dev`)**: the identity the CLI uses. It has an access key (an ID and a secret) stored on this machine. Never commit or paste the secret.
- **Profile (`default`)**: a named set of credentials + settings, stored in `~/.aws/credentials` and `~/.aws/config`.
- **Region (`us-east-1`, N. Virginia)**: the AWS data centre region. An account is not tied to a region: you choose one per resource, and the CLI's `region` setting decides where commands run (the region selector in the web console is separate and only affects what the browser shows). Resources are only visible in the region they were created in, so we use one region for everything. We chose `us-east-1` instead of the plan's original `ap-south-1`; both support every service we need, and `us-east-1` is usually the cheapest. IAM is the one global service.
- **Free plan**: limited credits, and some services (e.g. Redshift) are blocked. We verify the rest now.

## Steps

> Masking rule: when pasting output into this doc, replace the 12-digit account ID with `<ACCOUNT_ID>`.

### Step 0 — Install the AWS CLI (needed because `aws` was "not recognized")

**What happened:** running `aws --version` in PowerShell gave `The term 'aws' is not recognized`. PowerShell looks for a program called `aws` in every folder listed in the `PATH` environment variable, and found nothing. We confirmed with a read-only check that `C:\Program Files\Amazon\AWSCLIV2\aws.exe` does not exist and the `~\.aws` settings folder does not exist, so the CLI was never installed (and so never configured).

**Why we install it this way:** `winget` is Windows' built-in package manager. It downloads the official AWS installer and registers the program in `PATH`, so we don't edit anything by hand.

```powershell
winget install --id Amazon.AWSCLI -e
```
- `winget install`: install a package.
- `--id Amazon.AWSCLI`: the exact package identifier (names can be ambiguous, ids are not).
- `-e` (`--exact`): match the id exactly, so winget never installs a similarly named package.
- If asked to accept source/package agreements, type `Y`.

**Important:** after the install, **close the VS Code terminal and open a new one** (or restart VS Code). A terminal reads `PATH` once when it starts, so an already-open terminal will not see the new program.

Then check it worked:
```powershell
aws --version
```

### Step 0b — Give the CLI your credentials (`aws configure`)

**Why:** the CLI is only a messenger. Every request must be signed with an **access key** (an ID + a secret) that belongs to an IAM user. `aws configure` stores them on this laptop so you don't type them each time.

**Prerequisite (done in the AWS web console, one time only):** IAM → Users → `jared-dev` → *Security credentials* → *Create access key* → choose *Command Line Interface (CLI)*. Copy the **Access key ID** and **Secret access key** (the secret is shown only once). Never paste the secret into chat, a notebook or Git.

```powershell
aws configure
```
It asks four questions:
| Prompt | Answer |
|---|---|
| AWS Access Key ID | the ID from the console |
| AWS Secret Access Key | the secret from the console |
| Default region name | `us-east-1` |
| Default output format | `json` |

**What it creates:** two files in `C:\Users\<you>\.aws\`:
- `credentials` — the key ID and secret (**sensitive**, stays on this laptop, never committed).
- `config` — region and output format (not sensitive).
They belong to the profile named `default`, which every command uses unless you pass `--profile other`.

### Step 1 — Is the CLI installed?
```powershell
aws --version
```
- `aws` runs the CLI; `--version` prints the installed version and exits without calling AWS.
- **Expected:** a line such as `aws-cli/2.x.x Python/3.x Windows/...`.

### Step 2 — Which profile and region are configured?
```powershell
aws configure list
```
- Shows where the CLI reads its settings from (profile, access key, secret key, region).
- Keys are **masked** (only the last 4 characters show), so the output is safe to read.
- **Expected:** `profile = default`, `region = us-east-1`.

### Step 3 — Who does AWS think I am?
```powershell
aws sts get-caller-identity
```
- `sts` = Security Token Service. `get-caller-identity` is a read-only call that works for any valid user and needs no extra permissions.
- **Expected:** JSON with `UserId`, `Account` and `Arn` ending in `user/jared-dev`.

## Results
| Step | Status | Output |
|---|---|---|
| 0 Install CLI | Done | `aws-cli/2.38.3 Python/3.14.6 Windows/11 exe/AMD64` (the Python shown is the one bundled inside the CLI, unrelated to our project's Python 3.12 venv) |
| 0b `aws configure` | Done (verified with Steps 2 and 3 below) | IAM user was created with the name `Ahmed` (not `jared-dev` as written in the plan; the name is only a label, so the plan's other names are unaffected). Access key created. |
| 1 `aws --version` | Done | see Step 0 |
| 2 `aws configure list` | Done | access/secret key loaded from `shared-credentials-file` (masked), `region = us-east-1` from `~/.aws/config`. `profile : <not set>` is normal: it only means no `AWS_PROFILE` variable is set, so the `default` profile is used. |
| 3 `aws sts get-caller-identity` | Done | `Account: <ACCOUNT_ID>`, `Arn: arn:aws:iam::<ACCOUNT_ID>:user/Ahmed` |
| 4 `aws freetier get-account-plan-state` | Done | `accountPlanType: FREE`, `accountPlanStatus: ACTIVE`, `accountPlanRemainingCredits: 118.08 USD`, `accountPlanExpirationDate: 2027-04-16` (account closes then, or earlier if credits run out) |
| 5 service checks | Done | `glue get-databases` → `"DatabaseList": []`, `lambda list-functions` → `"Functions": []`, `stepfunctions list-state-machines` → `"stateMachines": []`, `scheduler list-schedules` → `"Schedules": []`. All four services are available and nothing exists yet. |
| 6 budget | Done | `create-budget` exit code 0. `describe-budgets` → `pf-monthly-budget`, limit 50.0, `MONTHLY`. `describe-notifications-for-budget` → thresholds 5, 20, 50. |

### Step 4 — Free-plan status and credits
```powershell
aws freetier get-account-plan-state
```
- `freetier` is the service behind the Free plan; `get-account-plan-state` is read-only.
- Tells us the plan type and remaining credits, so we can track spend against the ~$110 budget.
- Note: the Free Tier API is served from `us-east-1`, which matches our region.

### Step 5 — Are the other services usable on the Free plan?
Each command is a read-only **list** call. If the service is available and we have permission, we get a list (possibly empty). If not, we get an error that tells us what is blocked.

```powershell
aws glue get-databases
aws lambda list-functions
aws stepfunctions list-state-machines
aws scheduler list-schedules
```
| Command | Service | Used for |
|---|---|---|
| `glue get-databases` | AWS Glue Data Catalog | the `pf_silver` / `pf_gold` databases, and the Spark job |
| `lambda list-functions` | AWS Lambda | the `pf-ingest` function |
| `stepfunctions list-state-machines` | AWS Step Functions | the `pf-daily-pipeline` workflow |
| `scheduler list-schedules` | EventBridge Scheduler | the daily trigger |

- **Expected:** an empty list for each, e.g. `{"Functions": []}`. Empty means "available, nothing created yet".
- `AccessDeniedException` = the IAM user `Ahmed` lacks permission (fixable by attaching a policy).
- A message about a plan/subscription = the Free plan blocks it (we would stop and adjust the design).

### Deviation from the plan: Athena `SELECT 1` is moved to the AWS foundation step
Athena stores every query result as a file in S3, so a query cannot run until a results bucket exists. Creating buckets belongs to the AWS foundation step, so we run the Athena test at the end of that step instead of here.

### Starting a new work session (no need to re-enter keys)
`aws configure` saved the key and region to `C:\Users\<you>\.aws\credentials` and `config`. These files stay on disk after you close VS Code or restart the laptop, so **you do not configure again**. To confirm you are still connected at the start of any session:
```powershell
aws sts get-caller-identity
```
- Shows the `Ahmed` ARN → connected.
- `Unable to locate credentials` → the `.aws` files were deleted or you are on a different laptop/user; run `aws configure` again.
- `InvalidClientTokenId` / `AccessDenied` → the access key was deleted or deactivated in IAM; create a new key and run `aws configure` again.

### Step 6 — Create the budget `pf-monthly-budget`

**Why:** AWS does not stop spending when credits run low; a budget only **emails** you. We create one so that spending of $5, $20 and $50 each trigger an email. Budgets are free (the first two budgets per account cost nothing).

**Files we created (in `infra/budget/`):**

| File | Purpose |
|---|---|
| `budget.json` | Defines the budget: name `pf-monthly-budget`, type `COST`, resets `MONTHLY`, limit `50 USD`. |
| `notifications.json` | Three alert rules (at 5, 20 and 50 USD). Each rule says: *when ACTUAL spend is GREATER_THAN the threshold, email this address.* It holds your real email, so it is **gitignored** and never goes to GitHub. (We first made an `.example.json` template with a placeholder; it was removed because the real file was filled in directly.) |

**Inside `budget.json`: the `CostTypes` block matters.** By default a budget subtracts credits from spend. Our whole bill is covered by credits, so the budget would show $0 forever and **no alert would ever fire**. `"IncludeCredit": false` (and `"IncludeRefund": false`) makes the budget measure the gross usage, so the alerts are real. Everything else in that block is left at its normal default.

**Inside the notifications file:** `NotificationType: ACTUAL` = real spend so far (the alternative `FORECASTED` predicts it). `ThresholdType: ABSOLUTE_VALUE` = the threshold is a dollar amount (the alternative is `PERCENTAGE` of the limit).

**Commands:**
```powershell
# 1. (done) infra\budget\notifications.json already contains your email

# 2. Save the account ID in a variable (no need to type it)
$ACCOUNT_ID = aws sts get-caller-identity --query Account --output text

# 3. Create the budget
aws budgets create-budget `
  --account-id $ACCOUNT_ID `
  --budget file://infra/budget/budget.json `
  --notifications-with-subscribers file://infra/budget/notifications.json

# 4. Verify
aws budgets describe-budgets --account-id $ACCOUNT_ID
```
- `--query Account` is a JMESPath filter that keeps only the `Account` field from the JSON; `--output text` removes the quotes. `$ACCOUNT_ID = ...` stores the result in a PowerShell variable for this terminal session.
- The backtick `` ` `` at the end of a line means "the command continues on the next line".
- `file://path` tells the CLI to read the value from a file instead of the command line (avoids PowerShell quoting problems with JSON).
- `create-budget` prints nothing when it succeeds. `describe-budgets` is how you confirm.

**`.gitignore` (created at the project root during the access checks):** it lists files Git must never track. The rules are grouped as: secrets and personal settings (`.env`, `*credentials*`, `config/settings.json`, `infra/budget/notifications.json`), Python noise (`.venv/`, `__pycache__/`), generated data (`data/`, `output/`, `*.jar`), build output (`*.zip`) and OS noise. In a `.gitignore`, a `#` comment must be on its own line; a comment after a pattern becomes part of the pattern and the rule silently stops working.

**Results of the commands:**
- `create-budget` printed nothing and exit code `0` (success).
- `describe-budgets` returned `pf-monthly-budget`, limit `50.0`, `MONTHLY`.
- `describe-notifications-for-budget` returned thresholds `20.0, 5.0, 50.0`: all three alerts exist.

## Step 7
_(added after Step 6 passes)_
