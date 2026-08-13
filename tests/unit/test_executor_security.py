"""Golden security suite — every statement here MUST be rejected.

These tests are permanent: any future change to the executor must keep them
green. They prove that even if the AST validator (M5) were bypassed entirely,
the connection itself cannot modify the database.
"""

import pytest

from app.core.errors import SQLExecutionError, SQLValidationError

FORBIDDEN_STATEMENTS = [
    "INSERT INTO dim_b3_item (item_code) VALUES ('EVIL')",
    "UPDATE fact_ai_sales_net SET net_revenue = 0",
    "DELETE FROM fact_ai_sales_net",
    "DROP TABLE dim_b3_item",
    "DROP VIEW vw_ai_sales_by_year",
    "CREATE TABLE evil (x)",
    "CREATE VIEW evil_v AS SELECT 1",
    "ALTER TABLE dim_b3_item ADD COLUMN evil TEXT",
    "PRAGMA journal_mode=DELETE",
    "PRAGMA writable_schema=1",
    "ATTACH DATABASE '/tmp/evil.db' AS evil",
    "DETACH DATABASE main",
    "VACUUM",
    "REINDEX",
    "ANALYZE",
    "CREATE TRIGGER t AFTER INSERT ON dim_b3_item BEGIN SELECT 1; END",
]


@pytest.mark.parametrize("sql", FORBIDDEN_STATEMENTS)
def test_forbidden_statement_rejected(executor, sql):
    with pytest.raises(SQLExecutionError) as exc_info:
        executor.run_sql(sql)
    # The public message must never echo SQL fragments back to the user.
    public = exc_info.value.public_message
    for fragment in ("INSERT", "DROP", "PRAGMA", "ATTACH", "TABLE", "evil"):
        assert fragment not in public


def test_multi_statement_rejected(executor):
    with pytest.raises(SQLExecutionError):
        executor.run_sql("SELECT 1; DELETE FROM fact_ai_sales_net")


def test_write_impossible_even_without_authorizer(fixture_db):
    """Layer 1 alone (mode=ro) must block writes if the authorizer were removed."""
    import sqlite3

    conn = sqlite3.connect(f"file:{fixture_db}?mode=ro", uri=True)
    with pytest.raises(sqlite3.OperationalError):
        conn.execute("DELETE FROM fact_ai_sales_net")
    conn.close()


def test_filter_value_injection_is_inert(executor):
    result = executor.run_view(
        "dim_b3_item", filters={"item_code": "x' OR '1'='1"}
    )
    assert result.row_count == 0  # bound as a literal value, matches nothing


def test_malicious_identifier_rejected(executor):
    for bad in ('dim_b3_item"; DROP TABLE x;--', "a b", "1abc", "", "vw-q1"):
        with pytest.raises(SQLValidationError):
            executor.run_view(bad)


def test_unknown_object_rejected(executor):
    with pytest.raises(SQLValidationError):
        executor.run_view("vw_q999_does_not_exist")


def test_timeout_aborts_runaway_query(executor):
    runaway = (
        "WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM c) "
        "SELECT COUNT(*) FROM c"
    )
    with pytest.raises(SQLExecutionError) as exc_info:
        executor.run_sql(runaway)
    assert "too long" in exc_info.value.public_message


def test_readonly_introspection_pragma_allowed(executor):
    result = executor.run_metadata_sql("SELECT name FROM pragma_table_info('dim_b3_item')")
    assert result.row_count > 0


def test_dangerous_pragmas_still_denied(executor):
    import pytest as _pytest

    for sql in ("PRAGMA journal_mode=DELETE", "PRAGMA writable_schema=1",
                "PRAGMA schema_version=99", "PRAGMA case_sensitive_like=1"):
        with _pytest.raises(SQLExecutionError):
            executor.run_sql(sql)
