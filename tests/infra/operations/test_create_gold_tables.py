"""Tests for infra/operations/create_gold_tables.py with fake Athena and Glue clients. No AWS needed."""
import pytest

from infra.operations import create_gold_tables as create_gold

CONFIG = {"lake_bucket": "my-lake", "workgroup": "pf-wg"}


class FakeGlue:
    class exceptions:                                    # noqa: N801 (boto3 spelling)
        class EntityNotFoundException(Exception):
            pass

    def __init__(self, existing=()):
        self.existing = set(existing)

    def get_table(self, DatabaseName, Name):             # noqa: N803
        if Name not in self.existing:
            raise self.exceptions.EntityNotFoundException()


class FakeAthena:
    def __init__(self, state="SUCCEEDED"):
        self.queries, self.state = [], state

    def start_query_execution(self, QueryString, WorkGroup):   # noqa: N803
        self.queries.append((WorkGroup, QueryString))
        return {"QueryExecutionId": str(len(self.queries))}

    def get_query_execution(self, QueryExecutionId):            # noqa: N803
        return {"QueryExecution": {"Status": {"State": self.state, "StateChangeReason": "boom"}}}


class FakeSession:
    def __init__(self, athena, glue):
        self.clients = {"athena": athena, "glue": glue}

    def client(self, name):
        return self.clients[name]


def test_the_tables_come_first_in_dependency_order_then_every_view():
    files = create_gold.gold_files()
    assert [table for table, _ in files[:5]] == ["dim_patient", "dim_facility", "dim_practitioner", "fact_encounter",
                                                 "dim_date"]
    assert len(files) == 10 and all(table is None and path.parent.name == "kpi_views" for table, path in files[5:])
    assert all(path.exists() for _, path in files)


def test_every_statement_runs_in_the_pipeline_workgroup_with_the_lake_bucket_filled_in():
    athena = FakeAthena()
    create_gold.create_gold(FakeSession(athena, FakeGlue()), CONFIG)

    assert {workgroup for workgroup, _ in athena.queries} == {"pf-wg"}
    text = " ".join(sql for _, sql in athena.queries)
    assert "s3://my-lake/gold/" in text and "{{" not in text
    assert text.count("CREATE OR REPLACE VIEW") == 5


def test_dim_date_is_not_built_again_when_it_exists():
    athena = FakeAthena()
    create_gold.create_gold(FakeSession(athena, FakeGlue(existing={"dim_date"})), CONFIG)

    assert not any("dim_date" in sql and "UNNEST" in sql for _, sql in athena.queries)


def test_a_failed_statement_stops_the_run_with_the_reason():
    with pytest.raises(RuntimeError, match="FAILED: boom"):
        create_gold.create_gold(FakeSession(FakeAthena(state="FAILED"), FakeGlue()), CONFIG)
