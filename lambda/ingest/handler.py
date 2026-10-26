"""Ingest Lambda (bronze layer): copy one landing batch into the bronze layer.

Two kinds of input event:
  {"batch": N}                      start a batch by hand, 1 <= N <= NUMBER_OF_BATCHES
  {"bucket": B, "key": "landing/batch_NN/_SUCCESS"}
                                    the delivery marker of a batch (sent by the EventBridge trigger).
                                    The marker file is JSON {"files": <count>}: the number of files the sender
                                    delivered. If the batch holds a different number, it is incomplete and the
                                    function fails instead of ingesting half a delivery.
Reads from:   s3://<bucket>/landing/batch_NN/            (the marker file itself is never copied)
Writes to:    s3://<bucket>/bronze/patient_records/ingest_date=<YYYY-MM-DD>/   (plus _batch_metadata.json)

Running the same batch again overwrites the same keys, so a retry never
creates duplicates.

Settings come from environment variables set at deploy time:
    LAKE_BUCKET, FIRST_INGEST_DATE (default 2026-11-01), NUMBER_OF_BATCHES (default 10)
"""
import json
import logging
import os
import re
from datetime import date, datetime, timedelta, timezone

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)


def log(message, **fields):
    """Write one JSON line per log entry, easy to search in CloudWatch Logs."""
    logger.info(json.dumps({"message": message, **fields}))


MARKER_NAME = "_SUCCESS"
MARKER_KEY = re.compile(r"landing/batch_(\d{2})/_SUCCESS")


def parse_batch(event, n_batches):
    """Return the batch number from the event, or raise ValueError if it is missing or out of range.

    The batch is either given directly ({"batch": N}) or taken from the key of a marker file
    ({"key": "landing/batch_NN/_SUCCESS"}); any other key is rejected.
    """
    if isinstance(event, dict) and "key" in event:
        match = MARKER_KEY.fullmatch(str(event["key"]))
        if not match:
            raise ValueError(f"key must look like landing/batch_NN/{MARKER_NAME}, got {event['key']!r}")
        batch = int(match.group(1))
    else:
        batch = event.get("batch") if isinstance(event, dict) else None
        # bool is a subclass of int in Python, so True would otherwise pass as batch 1
        if isinstance(batch, bool) or not isinstance(batch, int):
            raise ValueError(f'event must contain an integer "batch", got: {event!r}')
    if not 1 <= batch <= n_batches:
        raise ValueError(f"batch must be between 1 and {n_batches}, got {batch}")
    return batch


def read_expected_files(s3, bucket, key):
    """The number of files the sender says it delivered: the marker file holds JSON like {"files": 37}."""
    body = s3.get_object(Bucket=bucket, Key=key)["Body"].read()
    try:
        files = json.loads(body)["files"]
    except (ValueError, KeyError, TypeError):
        raise ValueError(f'marker s3://{bucket}/{key} must contain JSON like {{"files": 37}}') from None
    if isinstance(files, bool) or not isinstance(files, int) or files < 1:
        raise ValueError(f'marker s3://{bucket}/{key}: "files" must be a positive integer, got {files!r}')
    return files


def ingest_date_for(batch, first_ingest_date):
    """Simulated arrival date: the first batch arrives on the first date, one batch per day."""
    return first_ingest_date + timedelta(days=batch - 1)


def list_objects(s3, bucket, prefix):
    """All objects under a prefix (handles S3's 1000-keys-per-page limit)."""
    objects = []
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix):
        objects.extend(page.get("Contents", []))
    # Skip "folder" placeholder keys that end with a slash
    return [obj for obj in objects if not obj["Key"].endswith("/")]


def ingest_batch(s3, bucket, batch, first_ingest_date, expected_files=None):
    """Copy every file of the batch into the bronze layer and write a metadata file. Returns a summary.

    expected_files: the count announced by the marker file. If given, the batch must hold exactly that many files.
    """
    ingest_date = ingest_date_for(batch, first_ingest_date)
    source_prefix = f"landing/batch_{batch:02d}/"
    target_prefix = f"bronze/patient_records/ingest_date={ingest_date.isoformat()}/"

    # The marker is a delivery receipt, not data: it is not counted and not copied
    objects = [obj for obj in list_objects(s3, bucket, source_prefix) if not obj["Key"].endswith("/" + MARKER_NAME)]
    if not objects:
        raise RuntimeError(f"no files found under s3://{bucket}/{source_prefix}")
    if expected_files is not None and len(objects) != expected_files:
        raise RuntimeError(
            f"incomplete batch {batch}: the marker announces {expected_files} files but "
            f"s3://{bucket}/{source_prefix} holds {len(objects)}"
        )

    for obj in objects:
        # Server-side copy: the data never passes through the Lambda
        s3.copy_object(
            Bucket=bucket,
            Key=target_prefix + obj["Key"][len(source_prefix):],
            CopySource={"Bucket": bucket, "Key": obj["Key"]},
        )

    metadata = {
        "batch": batch,
        "ingest_date": ingest_date.isoformat(),
        "file_count": len(objects),
        "total_bytes": sum(obj["Size"] for obj in objects),
        "source_prefix": source_prefix,
        "ingested_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    s3.put_object(
        Bucket=bucket,
        Key=target_prefix + "_batch_metadata.json",
        Body=json.dumps(metadata).encode("utf-8"),
        ContentType="application/json",
    )
    return {"status": "OK", "batch": batch, "ingest_date": metadata["ingest_date"], "file_count": len(objects)}


def lambda_handler(event, context):
    """Entry point called by AWS Lambda."""
    bucket = os.environ["LAKE_BUCKET"]
    first_ingest_date = date.fromisoformat(os.environ.get("FIRST_INGEST_DATE", "2026-11-01"))
    n_batches = int(os.environ.get("NUMBER_OF_BATCHES", "10"))

    try:
        batch = parse_batch(event, n_batches)
        s3 = boto3.client("s3")
        expected_files = None
        if "key" in event:
            # Triggered by a marker file: only our own bucket is accepted, and the marker says how many files to expect
            if event.get("bucket") != bucket:
                raise ValueError(f"event is for bucket {event.get('bucket')!r}, this pipeline serves {bucket!r}")
            expected_files = read_expected_files(s3, bucket, event["key"])
        log("ingest started", batch=batch, expected_files=expected_files)
        result = ingest_batch(s3, bucket, batch, first_ingest_date, expected_files)
        log("ingest finished", **result)
        return result
    except Exception as error:
        log("ingest failed", error=str(error), event=event)
        raise  # a raised error marks the invocation as failed, so Step Functions can react
