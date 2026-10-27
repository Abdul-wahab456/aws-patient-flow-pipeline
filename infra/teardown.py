"""Delete AWS resources of this project. Two uses:

  1. retire the hand-built resources of Phases 3-10, so CloudFormation can create the same names (Phase 11)
  2. tear down the whole project when it is finished (Phase 14)

Nothing here runs by itself. Every function deletes ONE kind of resource and does nothing when it is already gone,
so a run that stopped half way can be started again. The notebooks print the plan and ask before they call these.

    python infra/teardown.py --plan                  # show what a full teardown would delete (deletes nothing)
    python infra/teardown.py --delete-everything     # asks you to type the lake bucket name first
"""
import argparse
import sys

import boto3
import deploy
import stacks
from botocore.exceptions import ClientError

NOT_FOUND = ("ResourceNotFoundException", "EntityNotFoundException", "NoSuchEntity", "NoSuchBucket", "404",
             "StateMachineDoesNotExist", "InvalidInputException", "ValidationError")


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


def hand_built_resources(config):
    """(kind, name) of everything the notebooks of Phases 3-10 created, in a safe deletion order."""
    names = stacks.resource_names(config["project_prefix"])
    prefix = config["project_prefix"]
    return [
        ("rule", f"{prefix}-new-batch-rule"),
        ("state_machine", names["state_machine"]),
        ("glue_job", names["glue_job"]),
        ("lambda", names["lambda_name"]),
        ("log_group", f"/aws/lambda/{names['lambda_name']}"),
        ("log_group", f"/aws/vendedlogs/states/{names['state_machine']}"),
        ("workgroup", names["workgroup"]),
        ("glue_database", "pf_gold"),
        ("glue_database", "pf_silver"),
        ("role", f"{prefix}-trigger-role"),
        ("role", f"{prefix}-step-functions-role"),
        ("role", f"{prefix}-glue-role"),
        ("role", f"{prefix}-lambda-role"),
        ("role", f"{prefix}-github-deploy-role"),
        ("bucket", config["results_bucket"]),
        ("bucket", config["lake_bucket"]),
    ]


def delete_rule(session, name):
    events = session.client("events")
    try:
        targets = [target["Id"] for target in events.list_targets_by_rule(Rule=name)["Targets"]]
        if targets:
            events.remove_targets(Rule=name, Ids=targets)
    except ClientError as error:
        if not _gone(error):
            raise
    return _quietly(lambda: events.delete_rule(Name=name), f"EventBridge rule {name}")


def delete_state_machine(session, name):
    sfn = session.client("stepfunctions")
    arns = [machine["stateMachineArn"] for page in sfn.get_paginator("list_state_machines").paginate()
            for machine in page["stateMachines"] if machine["name"] == name]
    for arn in arns:
        sfn.delete_state_machine(stateMachineArn=arn)
    print(f"{'deleted ' if arns else 'absent  '} state machine {name}")
    return bool(arns)


def delete_glue_job(session, name):
    return _quietly(lambda: session.client("glue").delete_job(JobName=name), f"Glue job {name}")


def delete_lambda(session, name):
    return _quietly(lambda: session.client("lambda").delete_function(FunctionName=name), f"Lambda {name}")


def delete_log_group(session, name):
    return _quietly(lambda: session.client("logs").delete_log_group(logGroupName=name), f"log group {name}")


def delete_workgroup(session, name):
    return _quietly(lambda: session.client("athena").delete_work_group(WorkGroup=name, RecursiveDeleteOption=True),
                    f"Athena workgroup {name}")


def delete_glue_database(session, name):
    """Deleting a catalog database also deletes the table definitions in it (not the files in S3)."""
    return _quietly(lambda: session.client("glue").delete_database(Name=name), f"Glue database {name}")


def delete_role(session, name):
    iam = session.client("iam")

    def remove():
        for policy in iam.list_attached_role_policies(RoleName=name)["AttachedPolicies"]:
            iam.detach_role_policy(RoleName=name, PolicyArn=policy["PolicyArn"])
        for policy_name in iam.list_role_policies(RoleName=name)["PolicyNames"]:
            iam.delete_role_policy(RoleName=name, PolicyName=policy_name)
        iam.delete_role(RoleName=name)

    return _quietly(remove, f"IAM role {name}")


def empty_and_delete_bucket(session, name):
    """Delete every object (and old version) of the bucket, then the bucket."""
    s3 = session.client("s3")

    def remove():
        s3.head_bucket(Bucket=name)
        paginator = s3.get_paginator("list_object_versions")
        count = 0
        for page in paginator.paginate(Bucket=name):
            items = [{"Key": item["Key"], "VersionId": item["VersionId"]}
                     for item in page.get("Versions", []) + page.get("DeleteMarkers", [])]
            if items:
                s3.delete_objects(Bucket=name, Delete={"Objects": items, "Quiet": True})
                count += len(items)
        print(f"         {count} objects removed from {name}")
        s3.delete_bucket(Bucket=name)

    return _quietly(remove, f"S3 bucket {name}")


DELETERS = {
    "rule": delete_rule, "state_machine": delete_state_machine, "glue_job": delete_glue_job, "lambda": delete_lambda,
    "log_group": delete_log_group, "workgroup": delete_workgroup, "glue_database": delete_glue_database,
    "role": delete_role, "bucket": empty_and_delete_bucket,
}


def delete_hand_built(session, config):
    """Delete the resources of hand_built_resources(); the ones that do not exist are skipped."""
    for kind, name in hand_built_resources(config):
        DELETERS[kind](session, name)


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
    waiter = cloudformation.get_waiter("stack_delete_complete")
    waiter.wait(StackName=name, WaiterConfig={"Delay": 10, "MaxAttempts": 90})
    print(f"deleted  stack {name}")
    return True


def teardown_plan(config):
    """What a full teardown deletes, in order. The buckets are kept by their stack on purpose and removed last."""
    names = stacks.stack_names(config["project_prefix"])
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

    config = deploy.load_config()
    plan = teardown_plan(config)
    print("a full teardown deletes, in this order:")
    for kind, name in plan:
        print(f"  {kind:<7} {name}")
    if args.plan or not args.delete_everything:
        return
    if input(f"type the lake bucket name ({config['lake_bucket']}) to confirm: ").strip() != config["lake_bucket"]:
        sys.exit("not confirmed; nothing was deleted")
    teardown_everything(boto3.Session(region_name=config["region"]), config)


if __name__ == "__main__":
    main()
