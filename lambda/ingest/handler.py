"""Ingest Lambda (bronze layer): copy one landing batch into the bronze layer.

Input event:  {"batch": N}   with 1 <= N <= NUMBER_OF_BATCHES
Reads from:   s3://<bucket>/landing/batch_NN/
Writes to:    s3://<bucket>/bronze/patient_records/ingest_date=<YYYY-MM-DD>/   (plus _batch_metadata.json)

Running the same batch again overwrites the same keys, so a retry never
creates duplicates.

Settings come from environment variables set at deploy time:
    LAKE_BUCKET, FIRST_INGEST_DATE (default 2026-11-01), NUMBER_OF_BATCHES (default 10)
"""
import json
import logging
import os
from datetime import date, datetime, timedelta, timezone

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)


def log(message, **fields):
    """Write one JSON line per log entry, easy to search in CloudWatch Logs."""
    logger.info(json.dumps({"message": message, **fields}))


def parse_batch(event, n_batches):
    """Return the batch number from the event, or raise ValueError if it is missing or out of range."""
    batch = event.get("batch") if isinstance(event, dict) else None
    # bool is a subclass of int in Python, so True would otherwise pass as batch 1
    if isinstance(batch, bool) or not isinstance(batch, int):
        raise ValueError(f'event must contain an integer "batch", got: {event!r}')
    if not 1 <= batch <= n_batches:
        raise ValueError(f"batch must be between 1 and {n_batches}, got {batch}")
    return batch


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


def ingest_batch(s3, bucket, batch, first_ingest_date):
    """Copy every file of the batch into the bronze layer and write a metadata file. Returns a summary."""
    ingest_date = ingest_date_for(batch, first_ingest_date)
    source_prefix = f"landing/batch_{batch:02d}/"
    target_prefix = f"bronze/patient_records/ingest_date={ingest_date.isoformat()}/"

    objects = list_objects(s3, bucket, source_prefix)
    if not objects:
        raise RuntimeError(f"no files found under s3://{bucket}/{source_prefix}")

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
        log("ingest started", batch=batch)
        result = ingest_batch(boto3.client("s3"), bucket, batch, first_ingest_date)
        log("ingest finished", **result)
        return result
    except Exception as error:
        log("ingest failed", error=str(error), event=event)
        raise  # a raised error marks the invocation as failed, so Step Functions can react
