"""Create the gold tables and the five KPI views in Athena. Run it once after the foundation stack exists:

    python -m infra.operations.create_gold_tables

It runs sql/gold/create_tables/*.sql (the Iceberg tables) and then sql/gold/kpi_views/*.sql (the KPI views).
It is safe to run again:
the tables are created only if they are missing, and the views are replaced. dim_date is built from scratch
(one row for every day from 1900 to 2030), so it is skipped when it already exists.

Settings come from the same places as infra/shared/settings.py (environment variables, then config/settings.json).
"""
import time

import boto3

from infra.shared import settings
from infra.shared.sql_files import read_statements

# The dimensions first, then the fact table that points at them; dim_date is last because it is built from scratch
DDL_ORDER = ("dim_patient", "dim_facility", "dim_practitioner", "fact_encounter", "dim_date")
GOLD_DATABASE = "pf_gold"      # the SQL files name this database, so it does not change with the project prefix


def gold_files(root=settings.ROOT):
    """(table name or None, path) in the order they must run: the tables first, then every view."""
    ddl = [(name, root / "sql" / "gold" / "create_tables" / f"{name}.sql") for name in DDL_ORDER]
    views = [(None, path) for path in sorted((root / "sql" / "gold" / "kpi_views").glob("*.sql"))]
    return ddl + views


def run_statement(athena, workgroup, sql, timeout=300):
    """Run one statement and wait until it has finished; raise if it failed."""
    # Athena is asynchronous: starting a query only returns its id, so we ask again every second until it is final
    query_id = athena.start_query_execution(QueryString=sql, WorkGroup=workgroup)["QueryExecutionId"]
    deadline = time.time() + timeout
    while True:
        status = athena.get_query_execution(QueryExecutionId=query_id)["QueryExecution"]["Status"]
        if status["State"] in ("SUCCEEDED", "FAILED", "CANCELLED"):
            break
        if time.time() > deadline:
            raise TimeoutError(f"Athena query {query_id} still {status['State']} after {timeout} s")
        time.sleep(1)
    if status["State"] != "SUCCEEDED":
        raise RuntimeError(f"Athena query {status['State']}: {status.get('StateChangeReason', 'no reason given')}")


def table_exists(glue, database, name):
    """True if the table is in the Glue catalog (the catalog is the index Athena uses to find tables)."""
    try:
        glue.get_table(DatabaseName=database, Name=name)
        return True
    except glue.exceptions.EntityNotFoundException:
        return False


def create_gold(session, config, root=settings.ROOT):
    athena, glue = session.client("athena"), session.client("glue")
    for table, path in gold_files(root):
        label = f"{'table' if table else 'view '} {table or path.stem}"
        # dim_date is created with CREATE TABLE ... AS SELECT (one row per day, 1900-2030), which fails if it exists;
        # the other tables use CREATE TABLE IF NOT EXISTS and views use CREATE OR REPLACE, so those are safe to repeat
        if table == "dim_date" and table_exists(glue, GOLD_DATABASE, table):
            print(f"exists   {label}")
            continue
        for statement in read_statements(path, {"LAKE_BUCKET": config["lake_bucket"]}):
            run_statement(athena, config["workgroup"], statement)
        print(f"created  {label}")


def main():
    config = settings.load_config()
    create_gold(boto3.Session(region_name=config["region"]), config)


if __name__ == "__main__":
    main()
