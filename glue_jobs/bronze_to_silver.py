"""Glue job: bronze -> silver.

Reads one batch of raw FHIR files from
    s3://<lake_bucket>/bronze/patient_records/ingest_date=<ingest_date>/
and writes five Parquet tables to
    s3://<lake_bucket>/silver/<table>/ingest_date=<ingest_date>/
registering each table and partition in the Glue Data Catalog database <silver_database>.

Job arguments (all required):
    --ingest_date       the batch to process, YYYY-MM-DD
    --lake_bucket       name of the data lake bucket
    --silver_database   Glue database for the silver tables

Safe to run again for the same ingest_date: the partition folder is overwritten and the
partition is registered only if it is missing, so a rerun gives the same tables.

The transformation logic lives in fhir_utils.py (tested on a laptop); this file only deals with AWS.
"""
import sys

import boto3
import fhir_utils as fu
from awsglue.context import GlueContext
from awsglue.job import Job
from awsglue.utils import getResolvedOptions
from pyspark.context import SparkContext

# Tables that must contain rows for the job to count as successful
MUST_HAVE_ROWS = ("patient", "encounter")


def list_keys(bucket, prefix):
    """All object keys under a prefix."""
    keys = []
    for page in boto3.client("s3").get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix):
        keys.extend(obj["Key"] for obj in page.get("Contents", []))
    return keys


def build_tables(spark, bucket, files):
    """Read the bronze files and build the silver DataFrames (nothing is written yet)."""
    def uris(keys):
        return [f"s3://{bucket}/{key}" for key in keys]

    patient_resources = fu.read_resources(spark, uris(files["patients"]))
    tables = {
        "patient": fu.patient_table(patient_resources),
        "encounter": fu.encounter_table(patient_resources),
        "condition": fu.condition_table(patient_resources),
    }
    # The hospital and practitioner lists arrive with the first batch only
    if files["hospital"]:
        tables["organization"] = fu.organization_table(fu.read_resources(spark, uris(files["hospital"])))
    else:
        print("no hospital file in this batch: organization table not written")
    if files["practitioners"]:
        tables["practitioner"] = fu.practitioner_table(fu.read_resources(spark, uris(files["practitioners"])))
    else:
        print("no practitioner file in this batch: practitioner table not written")
    return tables


def check_tables(tables):
    """Cache each table, check it, and return its row count. Raises before anything is written."""
    counts = {}
    for name, df in tables.items():
        df.cache()
        fu.require_no_nulls(df, fu.REQUIRED_COLUMNS[name], name)
        counts[name] = df.count()
        if counts[name] == 0 and (name in MUST_HAVE_ROWS or name in ("organization", "practitioner")):
            raise RuntimeError(f"{name}: 0 rows, the input files contain no {name} resources")
    return counts


def write_table(spark, name, df, expected_rows, bucket, database, ingest_date):
    """Write one partition (overwriting it) and register the table and the partition in the catalog."""
    location = f"s3://{bucket}/silver/{name}/"
    partition_path = f"{location}ingest_date={ingest_date}/"

    # ingest_date is not a column inside the files: it is the folder name, which Athena reads as the partition
    df.write.mode("overwrite").parquet(partition_path)

    written = spark.read.parquet(partition_path).count()
    if written != expected_rows:
        raise RuntimeError(f"{name}: wrote {written} rows but expected {expected_rows}")

    spark.sql(fu.table_ddl(database, name, fu.columns_of(df), location))
    spark.sql(
        f"ALTER TABLE `{database}`.`{name}` ADD IF NOT EXISTS "
        f"PARTITION (ingest_date='{ingest_date}') LOCATION '{partition_path}'"
    )

    # Prove the partition is in the Glue catalog (if the job were not using it, this would fail)
    partitions = [row[0] for row in spark.sql(f"SHOW PARTITIONS `{database}`.`{name}`").collect()]
    if f"ingest_date={ingest_date}" not in partitions:
        raise RuntimeError(f"{name}: partition ingest_date={ingest_date} is not in the Glue catalog")

    print(f"ROWCOUNT table={name} ingest_date={ingest_date} rows={expected_rows}")


def main():
    args = getResolvedOptions(sys.argv, ["JOB_NAME", "ingest_date", "lake_bucket", "silver_database"])
    ingest_date = fu.validate_ingest_date(args["ingest_date"])
    bucket, database = args["lake_bucket"], args["silver_database"]

    glue_context = GlueContext(SparkContext())
    spark = glue_context.spark_session
    job = Job(glue_context)
    job.init(args["JOB_NAME"], args)

    # Store timestamps as UTC instants in a format Athena reads as `timestamp`
    spark.conf.set("spark.sql.session.timeZone", "UTC")
    spark.conf.set("spark.sql.parquet.outputTimestampType", "TIMESTAMP_MICROS")

    bronze_prefix = f"bronze/patient_records/ingest_date={ingest_date}/"
    files = fu.classify_bronze_files(list_keys(bucket, bronze_prefix))
    print(
        f"bronze files in s3://{bucket}/{bronze_prefix}: patients={len(files['patients'])} "
        f"hospital={len(files['hospital'])} practitioners={len(files['practitioners'])}"
    )
    if not files["patients"]:
        raise RuntimeError(f"no patient files under s3://{bucket}/{bronze_prefix}: was the batch ingested?")

    tables = build_tables(spark, bucket, files)
    counts = check_tables(tables)

    for name, df in tables.items():
        write_table(spark, name, df, counts[name], bucket, database, ingest_date)

    job.commit()


main()
