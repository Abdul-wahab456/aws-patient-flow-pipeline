"""Delete everything this project created in AWS: the stacks, then the two buckets (the stacks keep them on purpose).

Nothing here runs by itself. Every function deletes ONE kind of resource and does nothing when it is already gone,
so a run that stopped half way can be started again.

    python -m infra.operations.teardown --plan                # show what a full teardown would delete (deletes nothing)
    python -m infra.operations.teardown --delete-everything   # asks you to type the lake bucket name first
"""
import argparse
import sys

import boto3
from botocore.exceptions import ClientError

from infra.github_actions import deploy_stacks
from infra.shared import settings

NOT_FOUND = ("NoSuchBucket", "404", "ValidationError")


def _gone(error):
    """True when an AWS error only means "this does not exist (any more)"."""
    code = error.response["Error"]["Code"]
    return code in NOT_FOUND or "NotFound" in code or "DoesNotExist" in code


def _quietly(action, label):
    """Run one delete call; say whether it deleted something or found nothing."""
    try:
        action()
        print(f"deleted  {label}")
        return True
    except ClientError as error:
        if _gone(error):
            print(f"absent   {label}")
            return False
        raise


def empty_and_delete_bucket(session, name):
    """Delete every object (and old version) of the bucket, then the bucket."""
    s3 = session.client("s3")

    def remove():
        # head_bucket fails with 404 when the bucket is already gone, which _quietly reports as "absent"
        s3.head_bucket(Bucket=name)
        # A bucket can only be deleted when it is empty, including old versions and delete markers
        paginator = s3.get_paginator("list_object_versions")
        count = 0
        for page in paginator.paginate(Bucket=name):
            items = [{"Key": item["Key"], "VersionId": item["VersionId"]}
                     for item in page.get("Versions", []) + page.get("DeleteMarkers", [])]
            if items:                      # one listing page holds at most 1000 keys, the limit of delete_objects
                s3.delete_objects(Bucket=name, Delete={"Objects": items, "Quiet": True})
                count += len(items)
        print(f"         {count} objects removed from {name}")
        s3.delete_bucket(Bucket=name)

    return _quietly(remove, f"S3 bucket {name}")


def delete_stack(session, name):
    cloudformation = session.client("cloudformation")
    try:
        cloudformation.describe_stacks(StackName=name)
    except ClientError as error:
        if _gone(error):
            print(f"absent   stack {name}")
            return False
        raise
    cloudformation.delete_stack(StackName=name)
    # Wait until the stack is really gone: the next one in the plan may depend on it (the pipeline needs the foundation)
    waiter = cloudformation.get_waiter("stack_delete_complete")
    waiter.wait(StackName=name, WaiterConfig={"Delay": 10, "MaxAttempts": 90})
    print(f"deleted  stack {name}")
    return True


def teardown_plan(config):
    """What a full teardown deletes, in order. The buckets are kept by their stack on purpose and removed last."""
    # Order: the pipeline first (it uses the foundation), then the foundation, then the buckets (the stacks keep them on
    # purpose so that deleting a stack never deletes data), and the CI/CD roles last.
    names = deploy_stacks.stack_names(config["project_prefix"])
    return [("stack", names["pipeline"]), ("stack", names["foundation"]), ("bucket", config["results_bucket"]),
            ("bucket", config["lake_bucket"]), ("stack", f"{config['project_prefix']}-cicd")]


def teardown_everything(session, config):
    for kind, name in teardown_plan(config):
        delete_stack(session, name) if kind == "stack" else empty_and_delete_bucket(session, name)


def main():
    parser = argparse.ArgumentParser(description="Show or run the full teardown of the project.")
    parser.add_argument("--plan", action="store_true", help="only print what would be deleted")
    parser.add_argument("--delete-everything", action="store_true", help="delete it (asks for the lake bucket name)")
    args = parser.parse_args()

    config = settings.load_config()
    plan = teardown_plan(config)
    print("a full teardown deletes, in this order:")
    for kind, name in plan:
        print(f"  {kind:<7} {name}")
    if args.plan or not args.delete_everything:
        return
    # A deliberate speed bump: typing the bucket name proves you know what is about to be deleted
    if input(f"type the lake bucket name ({config['lake_bucket']}) to confirm: ").strip() != config["lake_bucket"]:
        sys.exit("not confirmed; nothing was deleted")
    teardown_everything(boto3.Session(region_name=config["region"]), config)


if __name__ == "__main__":
    main()
