"""Checks on the SQL files and the SQL helper functions. No AWS needed."""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "infra"))
import sql_files  # noqa: E402

DDL = sorted((ROOT / "sql" / "gold" / "ddl").glob("*.sql"))
MERGES = sorted((ROOT / "sql" / "gold" / "merge").glob("*.sql"))
VIEWS = sorted((ROOT / "sql" / "gold" / "views").glob("*.sql"))
PLACEHOLDERS = {"LAKE_BUCKET": "my-lake"}


# ---- helper functions ------------------------------------------------------------------------------------
def test_render_fills_placeholders():
    assert sql_files.render("LOCATION 's3://{{LAKE_BUCKET}}/x/'", {"LAKE_BUCKET": "b"}) == "LOCATION 's3://b/x/'"


def test_render_fails_when_a_value_is_missing():
    with pytest.raises(ValueError, match="OTHER"):
        sql_files.render("{{LAKE_BUCKET}} {{OTHER}}", {"LAKE_BUCKET": "b"})


def test_split_statements_drops_comments_and_empty_parts():
    sql = "-- a comment; with a semicolon\nDROP TABLE a;\n\n-- another\nCREATE TABLE b (x int);\n"
    assert sql_files.split_statements(sql) == ["DROP TABLE a", "CREATE TABLE b (x int)"]


def test_count_parameters():
    assert sql_files.count_parameters("SELECT 1") == 0
    assert sql_files.count_parameters("... WHERE d = ? AND e = ?") == 2


def test_quote_date_accepts_a_date_and_rejects_anything_else():
    assert sql_files.quote_date("2026-11-01") == "'2026-11-01'"
    for bad in ("2026-11-01'; DROP TABLE x; --", "20261101", "", None):
        with pytest.raises(ValueError):
            sql_files.quote_date(bad)


def test_parse_result_rows_splits_header_and_data():
    rows = [{"Data": [{"VarCharValue": "id"}, {"VarCharValue": "name"}]},
            {"Data": [{"VarCharValue": "1"}, {"VarCharValue": "Ann"}]},
            {"Data": [{"VarCharValue": "2"}, {}]}]                     # a missing value comes back without VarCharValue
    assert sql_files.parse_result_rows(rows) == (["id", "name"], [["1", "Ann"], ["2", None]])


def test_parse_result_rows_accepts_a_statement_that_returns_no_rows():
    # CREATE TABLE / MERGE return nothing: this used to crash run_athena with an IndexError
    assert sql_files.parse_result_rows([]) == ([], [])


def test_parse_result_rows_without_a_header_keeps_every_row_as_data():
    # SHOW TABLES / SHOW DATABASES return no header row. Treating the first table name as a header hid dim_date.
    rows = [{"Data": [{"VarCharValue": "dim_date"}]}, {"Data": [{"VarCharValue": "dim_patient"}]}]
    assert sql_files.parse_result_rows(rows, has_header=False) == ([], [["dim_date"], ["dim_patient"]])


# ---- the SQL files ------------------------------------------------------------------------------------------
@pytest.mark.parametrize("path", DDL + MERGES + VIEWS, ids=lambda p: p.relative_to(ROOT).as_posix())
def test_every_sql_file_renders_and_has_exactly_one_statement(path):
    statements = sql_files.read_statements(path, PLACEHOLDERS)
    assert len(statements) == 1
    assert "{{" not in statements[0]


@pytest.mark.parametrize("path", DDL, ids=lambda p: p.name)
def test_ddl_creates_an_iceberg_table_in_the_gold_database(path):
    statement = sql_files.read_statements(path, PLACEHOLDERS)[0]
    assert statement.startswith("CREATE TABLE") and "pf_gold." in statement
    assert "ICEBERG" in statement.upper() and "s3://my-lake/gold/" in statement


@pytest.mark.parametrize("path", MERGES, ids=lambda p: p.name)
def test_merge_is_an_upsert_driven_by_the_batch_parameter(path):
    statement = sql_files.read_statements(path, PLACEHOLDERS)[0]
    assert statement.startswith("MERGE INTO pf_gold.")
    assert "WHEN MATCHED THEN UPDATE" in statement and "WHEN NOT MATCHED THEN INSERT" in statement
    assert "ROW_NUMBER()" in statement                        # one source row per key
    assert sql_files.count_parameters(statement) >= 1         # reads only the given ingest_date


def test_the_fact_merge_has_one_parameter_for_encounters_and_one_for_conditions():
    statement = sql_files.read_statements(ROOT / "sql/gold/merge/merge_fact_encounter.sql", PLACEHOLDERS)[0]
    assert sql_files.count_parameters(statement) == 2


@pytest.mark.parametrize("path", VIEWS, ids=lambda p: p.name)
def test_view_is_replaceable_and_named_like_its_file(path):
    statement = sql_files.read_statements(path, PLACEHOLDERS)[0]
    assert statement.startswith(f"CREATE OR REPLACE VIEW pf_gold.{path.stem} AS")
    assert sql_files.count_parameters(statement) == 0         # views do not depend on a batch date


def test_the_five_planned_kpi_views_exist():
    assert {p.stem for p in VIEWS} == {
        "vw_daily_admissions_discharges", "vw_avg_los_by_facility_month", "vw_readmission_30d",
        "vw_ed_volume_by_hour_dow", "vw_top_reasons",
    }


def test_the_fact_table_is_partitioned_by_year_not_by_month():
    # Athena allows at most 100 open partition writers per query; the visits span 1924-2026, so month partitions
    # (up to 352 in one batch) made the MERGE fail. Do not change this back to month.
    statement = sql_files.read_statements(ROOT / "sql/gold/ddl/fact_encounter.sql", PLACEHOLDERS)[0]
    assert "PARTITIONED BY (year(start_ts))" in statement


def test_the_date_dimension_covers_every_date_in_the_data():
    # The visits go back to 1924. The plan's first range (2015-2027) left old visits without a date row.
    import re
    statement = sql_files.read_statements(ROOT / "sql/gold/ddl/dim_date.sql", PLACEHOLDERS)[0]
    first, last = re.search(r"sequence\(DATE '(\d{4})-\d\d-\d\d', DATE '(\d{4})-\d\d-\d\d'", statement).groups()
    assert int(first) <= 1924 and int(last) >= 2026


def test_the_date_key_is_a_date_not_a_timestamp():
    # sequence() over dates gave timestamps in Athena: the key must be cast to a date
    statement = sql_files.read_statements(ROOT / "sql/gold/ddl/dim_date.sql", PLACEHOLDERS)[0]
    assert "CAST(d AS date) AS date_key" in " ".join(statement.split())


# ---- data-quality checks -------------------------------------------------------------------------------------
CHECKS = sorted((ROOT / "sql" / "dq").glob("*.sql"))


def test_there_are_checks_for_silver_and_for_gold():
    names = {p.stem for p in CHECKS}
    assert len(names) >= 14
    assert any(n.startswith("silver_") for n in names) and any(n.startswith("gold_") for n in names)


@pytest.mark.parametrize("path", CHECKS, ids=lambda p: p.name)
def test_a_check_returns_its_own_name_and_a_failure_count(path):
    statement = sql_files.read_statements(path, {})[0]
    assert statement.startswith(f"SELECT '{path.stem}' AS check_name, ")     # the name is the file name
    assert " AS failures" in statement
    assert "?" not in statement                                              # the date comes from the params CTE


BATCH_CHECKS = [p for p in CHECKS if p.stem.startswith("silver_") or p.stem == "gold_batch_missing_in_fact"]


@pytest.mark.parametrize("path", BATCH_CHECKS, ids=lambda p: p.name)
def test_a_check_about_one_batch_reads_the_batch_date_from_params(path):
    assert "(SELECT ingest_date FROM params)" in sql_files.read_statements(path, {})[0]


def test_the_combined_queries_have_exactly_one_date_parameter():
    statements = list(sql_files.read_checks(ROOT / "sql" / "dq").values())
    for query in (sql_files.detailed_checks_query(statements), sql_files.failed_checks_query(statements)):
        assert sql_files.count_parameters(query) == 1
        assert query.startswith("WITH params AS (SELECT ? AS ingest_date)")
        assert query.count("UNION ALL") == len(statements) - 1


def test_the_workflow_query_returns_ok_when_nothing_fails():
    query = sql_files.failed_checks_query(["SELECT 'a' AS check_name, 0 AS failures"])
    assert "'OK'" in query and "FILTER (WHERE failures > 0)" in query
