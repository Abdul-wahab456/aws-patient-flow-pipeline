"""Deliver a batch the way a sender does: write landing/batch_NN/_SUCCESS, last, with the number of files.

    python infra/release_batch.py --batch 2

EventBridge sees the marker and starts the workflow by itself. This script waits for that run and exits with an
error if it does not end SUCCEEDED. GitHub Actions runs it from the "Release batch" workflow (one click per batch).

Any batch from 1 to 99 can be released: there is no fixed number of batches. A new batch needs no setting,
no code change and no deployment: upload its files to landing/batch_NN/, then release it.

Settings (same as infra/deploy.py): LAKE_BUCKET, PROJECT_PREFIX, AWS_REGION or config/settings.json.
"""
import argparse
import json
import re
import sys
import time
from datetime import datetime, timedelta, timezone

import boto3
import deploy


def batch_number(text):
    """A batch number typed by a person (command line or GitHub form): a whole number from 1 to 99."""
    if not re.fullmatch(r"\d{1,2}", str(text).strip()) or not 1 <= int(text) <= 99:
        raise argparse.ArgumentTypeError(f"batch must be a number from 1 to 99, got {text!r}")
    return int(text)


def list_batches(s3, bucket):
    """The batch numbers that exist in landing/ (folders named batch_NN), in ascending order."""
    batches = set()
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix="landing/", Delimiter="/"):
        for prefix in page.get("CommonPrefixes", []):
            match = re.fullmatch(r"landing/batch_(\d{2})/", prefix["Prefix"])
            if match:
                batches.add(int(match.group(1)))
    return sorted(batches)


def count_batch_files(s3, bucket, batch):
    """The data files delivered to landing/batch_NN/ (the _SUCCESS marker is not a data file)."""
    prefix = f"landing/batch_{batch:02d}/"
    keys = [item["Key"]
            for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix)
            for item in page.get("Contents", [])]
    return len([key for key in keys if not key.endswith("/_SUCCESS")])


def find_started_run(sfn, state_machine_arn, since, attempts=30, pause=3):
    """The single execution that started after `since` (the rule starts it within seconds of the marker)."""
    started = []
    for _ in range(attempts):
        started = [run for run in sfn.list_executions(stateMachineArn=state_machine_arn, maxResults=20)["executions"]
                   if run["startDate"] > since]
        if started:
            break
        time.sleep(pause)
    if len(started) != 1:
        raise RuntimeError(f"expected exactly one execution to start, found {len(started)}")
    return started[0]


def wait_for_run(sfn, execution_arn, timeout=1500, poll=10):
    deadline = time.time() + timeout
    while True:
        run = sfn.describe_execution(executionArn=execution_arn)
        if run["status"] != "RUNNING":
            return run
        if time.time() > deadline:
            raise TimeoutError(f"{execution_arn} still running after {timeout} s")
        time.sleep(poll)


def release_batch(session, bucket, batch, state_machine_arn, announced_files=None, wait=True, timeout=1500):
    """Write the marker and return the run it started (finished, unless wait is False).

    announced_files replaces the real file count in the marker, to test an incomplete delivery.
    """
    s3, sfn = session.client("s3"), session.client("stepfunctions")
    count = announced_files if announced_files is not None else count_batch_files(s3, bucket, batch)
    if count == 0:
        raise ValueError(f"landing/batch_{batch:02d}/ holds no files: upload the batch first")
    moment = datetime.now(timezone.utc) - timedelta(seconds=2)
    s3.put_object(Bucket=bucket, Key=f"landing/batch_{batch:02d}/_SUCCESS", Body=json.dumps({"files": count}).encode(),
                  ContentType="application/json")
    run = find_started_run(sfn, state_machine_arn, moment)
    return wait_for_run(sfn, run["executionArn"], timeout=timeout) if wait else run


def main():
    parser = argparse.ArgumentParser(description="Release a batch and wait for the pipeline run it starts.")
    parser.add_argument("--batch", type=batch_number, required=True, help="batch number, 1 to 99")
    args = parser.parse_args()

    config = deploy.load_config()
    session = boto3.Session(region_name=config["region"])
    account = session.client("sts").get_caller_identity()["Account"]
    arn = f"arn:aws:states:{config['region']}:{account}:stateMachine:{config['project_prefix']}-batch-pipeline"

    run = release_batch(session, config["lake_bucket"], args.batch, arn)
    print(f"batch {args.batch}: {run['status']}")
    print(f"output: {run.get('output')}")
    if run["status"] != "SUCCEEDED":
        print(f"error : {run.get('error')}\ncause : {run.get('cause')}")
        sys.exit(1)


if __name__ == "__main__":
    main()
