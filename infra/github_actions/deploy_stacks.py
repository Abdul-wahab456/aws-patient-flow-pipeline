"""Deploy the pipeline as CloudFormation stacks.

    python -m infra.github_actions.deploy_stacks --version <commit id>

Three steps, in this order:
  1. stack <prefix>-foundation  (infra/cloudformation/foundation.yaml): buckets, catalog, workgroup, IAM roles
  2. the code is built and uploaded to the lake bucket: glue_scripts/<version>/ (Glue scripts) and
     artifacts/<version>/ (the Lambda zip and the finished workflow definition)
  3. stack <prefix>-pipeline    (infra/cloudformation/pipeline.yaml): Lambda, Glue job, workflow, EventBridge rule

Settings come from the same places as infra/shared/settings.py (environment variables, then config/settings.json).
GitHub Actions passes --role-arn so that CloudFormation creates everything as the CloudFormation service role,
not as the role that logs in from GitHub.
"""
import argparse
import json
import shutil
import subprocess
from pathlib import Path

import boto3

from infra.shared import pipeline_definitions, settings

TEMPLATES = settings.ROOT / "infra" / "cloudformation"
# CloudFormation refuses to create IAM roles with fixed names unless this is confirmed on purpose
CAPABILITIES = "CAPABILITY_NAMED_IAM"
# `aws` is on the PATH on GitHub and on most laptops; the second path is where Windows installs it
AWS_PROGRAM = shutil.which("aws") or r"C:\Program Files\Amazon\AWSCLIV2\aws.exe"


def stack_names(prefix):
    """The two stacks this script deploys. (The third, <prefix>-cicd, is deployed once by an administrator.)"""
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
    """The values foundation.yaml asks for when it is deployed."""
    return {
        "ProjectPrefix": config["project_prefix"],
        "LakeBucketName": config["lake_bucket"],
        "ResultsBucketName": config["results_bucket"],
    }


def pipeline_parameters(config, version):
    """The values pipeline.yaml asks for. DeploymentVersion tells it which uploaded code to run."""
    return {
        "ProjectPrefix": config["project_prefix"],
        "FoundationStackName": stack_names(config["project_prefix"])["foundation"],
        "DeploymentVersion": version,
        "FirstIngestDate": config["first_ingest_date"],
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
    # One folder per version, so building a new version never overwrites the files of an older one
    out_dir = Path(out_dir) / version
    out_dir.mkdir(parents=True, exist_ok=True)
    # The workflow must name the same Lambda, Glue job and workgroup that the templates create, hence the same prefix
    names = {**config, **resource_names(config["project_prefix"])}
    (out_dir / "ingest.zip").write_bytes(pipeline_definitions.lambda_package())
    (out_dir / "state_machine.json").write_text(
        json.dumps(pipeline_definitions.state_machine_definition(names, account)), encoding="utf-8")
    return {"ingest.zip": out_dir / "ingest.zip", "state_machine.json": out_dir / "state_machine.json"}


def upload_artifacts(session, config, version, built):
    """Upload the Glue scripts and the built files; the pipeline stack reads them from these keys."""
    s3, bucket = session.client("s3"), config["lake_bucket"]
    # Keys contain the version, so every deployment keeps its own copy and a rollback is "deploy the older version"
    uploads = {f"glue_scripts/{version}/{name}": settings.ROOT / "glue_jobs" / name
               for name in pipeline_definitions.GLUE_SCRIPTS}
    uploads.update({f"artifacts/{version}/{name}": path for name, path in built.items()})
    for key, path in uploads.items():
        s3.upload_file(str(path), bucket, key)
        print(f"uploaded s3://{bucket}/{key}")
    return list(uploads)


def run(command):
    """Run a command and stop the whole deployment if it fails (check=True raises on a non-zero exit code)."""
    print(" ".join(command))
    subprocess.run(command, check=True)


def main():
    parser = argparse.ArgumentParser(description="Deploy the foundation and pipeline stacks.")
    parser.add_argument("--version", required=True, help="name of this deployment, e.g. the Git commit id")
    parser.add_argument("--role-arn", help="CloudFormation service role that creates the resources")
    parser.add_argument("--build-dir", default=str(settings.ROOT / "build"), help="where the built files are written")
    args = parser.parse_args()

    config = settings.load_config()
    if not config["results_bucket"]:
        raise SystemExit("missing setting: RESULTS_BUCKET or athena_results_bucket in config/settings.json")
    session = boto3.Session(region_name=config["region"])
    account = session.client("sts").get_caller_identity()["Account"]
    names = stack_names(config["project_prefix"])

    # The order matters: the pipeline stack imports the foundation's roles and bucket, and the code has to be in the
    # bucket before the stack that points at it is created.
    run(deploy_command(names["foundation"], TEMPLATES / "foundation.yaml", foundation_parameters(config),
                       config["region"], args.role_arn))
    built = build_artifacts(config, account, args.version, args.build_dir)
    upload_artifacts(session, config, args.version, built)
    run(deploy_command(names["pipeline"], TEMPLATES / "pipeline.yaml", pipeline_parameters(config, args.version),
                       config["region"], args.role_arn))
    print("deployment finished")


if __name__ == "__main__":
    main()
