"""Helpers for the SQL files in sql/. Plain Python: no AWS and no settings, so they are easy to test.

Conventions used by the SQL files
- Placeholders written as {{NAME}} (e.g. {{LAKE_BUCKET}}) are replaced before the SQL is sent to Athena.
- The batch date is a parameter written as ? (Athena "execution parameters"); the same date fills every ?.
- Comments are whole lines starting with `--`. (A trailing comment or a ; inside a string would confuse the splitter.)
- A file can hold several statements, separated by ;
"""
import re
from pathlib import Path

# {{LAKE_BUCKET}}: values that differ per account are written as {{NAME}} in the SQL files and filled in when they run
PLACEHOLDER = re.compile(r"\{\{([A-Z_]+)\}\}")


def strip_comments(sql):
    """Remove the whole-line `--` comments."""
    # Whole-line comments only: a "--" inside a statement could belong to a string, so it is left alone
    return "\n".join(line for line in sql.splitlines() if not line.strip().startswith("--"))


def render(sql, placeholders):
    """Replace {{NAME}} with its value. Fails if a value is missing, instead of sending broken SQL."""
    missing = sorted(set(PLACEHOLDER.findall(sql)) - set(placeholders))
    if missing:
        raise ValueError(f"no value for placeholders: {missing}")
    return PLACEHOLDER.sub(lambda match: placeholders[match.group(1)], sql)


def split_statements(sql):
    """The statements of a SQL text (comments removed, empty parts dropped)."""
    # Athena runs one statement per call, so a file with several statements is cut at the semicolons
    return [part.strip() for part in strip_comments(sql).split(";") if part.strip()]


def count_parameters(statement):
    """How many ? parameters a statement has."""
    return statement.count("?")


def read_statements(path, placeholders):
    """Read a SQL file and return its statements with the placeholders filled in."""
    return split_statements(render(Path(path).read_text(encoding="utf-8"), placeholders))


def quote_date(value):
    """A batch date as an Athena parameter value: 'YYYY-MM-DD' including the quotes. Rejects anything else."""
    # The date is pasted into a query as a parameter, so it must be exactly a date and nothing else
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value or ""):
        raise ValueError(f"ingest_date must look like YYYY-MM-DD, got {value!r}")
    return f"'{value}'"


def parse_result_rows(rows, has_header=True):
    """Split the "Rows" of an Athena result into (header, data rows).

    A SELECT returns the column names as its first row (has_header=True). SHOW and DESCRIBE statements return
    no header row: every row is data (has_header=False). Statements that return nothing (CREATE TABLE, MERGE, ...)
    give no rows at all. A missing value comes back as None.
    """
    table = [[cell.get("VarCharValue") for cell in row["Data"]] for row in rows]
    if has_header and table:
        return table[0], table[1:]
    return [], table


# ---- data-quality checks (sql/data_quality_checks/silver and gold) ---------------------------------------------
# Each check file is one SELECT that returns one row: check_name, failures (0 = the check passes).
# Checks about ONE batch read its date from the CTE `params`, which the combined query defines once with the
# only ? parameter. That way the workflow passes a single date, however many checks there are.
PARAMS_CTE = "WITH params AS (SELECT ? AS ingest_date)"


def read_checks(folder):
    """The check files of a folder and its sub-folders (silver/, gold/) as {check name: statement}, sorted by name."""
    paths = sorted(Path(folder).rglob("*.sql"), key=lambda path: path.stem)
    return {path.stem: read_statements(path, {})[0] for path in paths}


def _union(statements):
    return "\nUNION ALL\n".join(statements)


def detailed_checks_query(statements):
    """One query returning one row per check: (check_name, failures). Used by the notebook."""
    return f"{PARAMS_CTE}\nSELECT check_name, failures FROM (\n{_union(statements)}\n)\nORDER BY check_name"


def failed_checks_query(statements):
    """One query returning one text value: the names of the failing checks, or 'OK' when all pass.

    Used by the workflow.
    """
    return (
        f"{PARAMS_CTE}\n"
        "SELECT COALESCE(array_join(array_agg(check_name ORDER BY check_name) FILTER (WHERE failures > 0), ', '), "
        f"'OK') AS failed_checks FROM (\n{_union(statements)}\n)"
    )
