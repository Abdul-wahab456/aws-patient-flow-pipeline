"""Deploy the pipeline as CloudFormation stacks.

    python infra/stacks.py --version <commit id>

Three steps, in this order:
  1. stack <prefix>-foundation  (infra/cloudformation/foundation.yaml): buckets, catalog, workgroup, IAM roles
  2. the code is built and uploaded to the lake bucket: glue_scripts/<version>/ (Glue scripts) and
     artifacts/<version>/ (the Lambda zip and the finished workflow definition)
  3. stack <prefix>-pipeline    (infra/cloudformation/pipeline.yaml): Lambda, Glue job, workflow, EventBridge rule

Settings come from the same places as infra/deploy.py (environment variables, then config/settings.json).
GitHub Actions passes --role-arn so that CloudFormation creates everything as the CloudFormation service role,
not as the role that logs in from GitHub.
"""
import argparse
import json
import shutil
import subprocess
from pathlib import Path

import boto3
import deploy

TEMPLATES = deploy.ROOT / "infra" / "cloudformation"
CAPABILITIES = "CAPABILITY_NAMED_IAM"
# `aws` is on the PATH on GitHub and on most laptops; the second path is where Windows installs it
AWS_PROGRAM = shutil.which("aws") or r"C:\Program Files\Amazon\AWSCLIV2\aws.exe"


def stack_names(prefix):
    return {"foundation": f"{prefix}-foundation", "pipeline": f"{prefix}-pipeline"}


def resource_names(prefix):
    """Names the templates give to the resources. The workflow definition has to use the same ones."""
    return {
        "lambda_name": f"{prefix}-ingest",
        "glue_job": f"{prefix}-bronze-to-silver",
        "workgroup": f"{prefix}-wg",
        "state_machine": f"{prefix}-batch-pipeline",
    }


def foundation_parameters(config):
    return {
        "ProjectPrefix": config["project_prefix"],
        "LakeBucketName": config["lake_bucket"],
        "ResultsBucketName": config["results_bucket"],
    }


def pipeline_parameters(config, version):
    return {
        "ProjectPrefix": config["project_prefix"],
        "FoundationStackName": stack_names(config["project_prefix"])["foundation"],
        "DeploymentVersion": version,
        "FirstIngestDate": config["first_ingest_date"],
        "NumberOfBatches": config["number_of_batches"],
    }


def deploy_command(stack, template, parameters, region, role_arn=None):
    """The `aws cloudformation deploy` command: creates the stack, or updates it when it exists.

    deploy builds a change set and executes it, and does not fail when there is nothing to change.
    """
    command = [
        AWS_PROGRAM, "cloudformation", "deploy", "--region", region, "--stack-name", stack,
        "--template-file", str(template), "--capabilities", CAPABILITIES,
        "--no-fail-on-empty-changeset", "--tags", "project=patient-flow",
        "--parameter-overrides", *[f"{key}={value}" for key, value in parameters.items()],
    ]
    if role_arn:
        command += ["--role-arn", role_arn]
    return command


def build_artifacts(config, account, version, out_dir):
    """Write the Lambda zip and the finished workflow definition (the ASL file with its SQL filled in) to out_dir."""
    out_dir = Path(out_dir) / version
    out_dir.mkdir(parents=True, exist_ok=True)
    names = {**config, **resource_names(config["project_prefix"])}
    (out_dir / "ingest.zip").write_bytes(deploy.lambda_package())
    (out_dir / "state_machine.json").write_text(
        json.dumps(deploy.state_machine_definition(names, account)), encoding="utf-8")
    return {"ingest.zip": out_dir / "ingest.zip", "state_machine.json": out_dir / "state_machine.json"}


def upload_artifacts(session, config, version, built):
    """Upload the Glue scripts and the built files; the pipeline stack reads them from these keys."""
    s3, bucket = session.client("s3"), config["lake_bucket"]
    uploads = {f"glue_scripts/{version}/{name}": deploy.ROOT / "glue_jobs" / name for name in deploy.GLUE_SCRIPTS}
    uploads.update({f"artifacts/{version}/{name}": path for name, path in built.items()})
    for key, path in uploads.items():
        s3.upload_file(str(path), bucket, key)
        print(f"uploaded s3://{bucket}/{key}")
    return list(uploads)


def run(command):
    print(" ".join(command))
    subprocess.run(command, check=True)


def main():
    parser = argparse.ArgumentParser(description="Deploy the foundation and pipeline stacks.")
    parser.add_argument("--version", required=True, help="name of this deployment, e.g. the Git commit id")
    parser.add_argument("--role-arn", help="CloudFormation service role that creates the resources")
    parser.add_argument("--build-dir", default=str(deploy.ROOT / "build"), help="where the built files are written")
    args = parser.parse_args()

    config = deploy.load_config()
    if not config["results_bucket"]:
        raise SystemExit("missing setting: RESULTS_BUCKET or athena_results_bucket in config/settings.json")
    session = boto3.Session(region_name=config["region"])
    account = session.client("sts").get_caller_identity()["Account"]
    names = stack_names(config["project_prefix"])

    run(deploy_command(names["foundation"], TEMPLATES / "foundation.yaml", foundation_parameters(config),
                       config["region"], args.role_arn))
    built = build_artifacts(config, account, args.version, args.build_dir)
    upload_artifacts(session, config, args.version, built)
    run(deploy_command(names["pipeline"], TEMPLATES / "pipeline.yaml", pipeline_parameters(config, args.version),
                       config["region"], args.role_arn))
    print("deployment finished")


if __name__ == "__main__":
    main()
