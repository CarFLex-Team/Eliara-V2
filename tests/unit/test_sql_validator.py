"""Golden suite for the AST validator — permanent regression net.

Every statement in REJECTED must fail forever; every statement in ACCEPTED
must pass forever. Any validator change is measured against this file.
"""

import pytest

from app.core.errors import SQLValidationError
from app.sqlgen.validator import validate_sql

WHITELIST = {
    "fact_ai_sales_net": {
        "customer_code", "customer_name", "item_code", "item_name",
        "warehouse_name", "net_revenue", "net_gross_profit", "net_quantity",
        "posting_date_iso", "year", "year_month",
    },
    "dim_b3_item": {"item_code", "item_name", "item_group_code", "total_on_hand"},
    "engine_margin_base": {"item_code", "margin_pct"},
}

REJECTED = [
    # --- writes / DDL ---
    "INSERT INTO fact_ai_sales_net (customer_code) VALUES ('x')",
    "UPDATE fact_ai_sales_net SET net_revenue = 0",
    "DELETE FROM fact_ai_sales_net WHERE 1=1",
    "DROP TABLE fact_ai_sales_net",
    "DROP VIEW dim_b3_item",
    "CREATE TABLE evil (x INT)",
    "CREATE VIEW v AS SELECT customer_code FROM fact_ai_sales_net",
    "ALTER TABLE dim_b3_item ADD COLUMN evil TEXT",
    "CREATE TRIGGER t AFTER INSERT ON dim_b3_item BEGIN SELECT 1; END",
    "CREATE INDEX ix ON fact_ai_sales_net(customer_code)",
    # --- admin / escape hatches ---
    "PRAGMA writable_schema = 1",
    "PRAGMA table_info(fact_ai_sales_net)",
    "ATTACH DATABASE '/tmp/x.db' AS x",
    "VACUUM",
    "REINDEX",
    "EXPLAIN QUERY PLAN SELECT customer_code FROM fact_ai_sales_net",
    # --- multi-statement / smuggling ---
    "SELECT customer_code FROM fact_ai_sales_net; SELECT 2",
    "SELECT customer_code FROM fact_ai_sales_net; DROP TABLE dim_b3_item",
    # --- outside the whitelist ---
    "SELECT * FROM sqlite_master",
    "SELECT * FROM chatbot_question_view_registry",
    "SELECT * FROM sap_oitm_raw",
    "SELECT * FROM batch_13_business_glossary",
    "SELECT secret FROM fact_ai_sales_net",
    "SELECT customer_code, password FROM fact_ai_sales_net",
    "SELECT f.customer_code FROM fact_ai_sales_net f JOIN unknown_table u ON 1=1",
    "SELECT (SELECT x FROM hidden_table) FROM fact_ai_sales_net",
    # --- dangerous functions ---
    "SELECT load_extension('/tmp/evil.so')",
    "SELECT readfile('/etc/passwd')",
    "SELECT writefile('/tmp/x', customer_code) FROM fact_ai_sales_net",
    # --- non-portable joins (SQLite build-dependent, real production failure) ---
    ("SELECT f.customer_code, d.item_name FROM fact_ai_sales_net f "
    "FULL OUTER JOIN dim_b3_item d ON f.item_code = d.item_code"),
    ("SELECT f.customer_code, d.item_name FROM fact_ai_sales_net f "
    "RIGHT JOIN dim_b3_item d ON f.item_code = d.item_code"),
    # --- not SQL / garbage ---
    "hello there",
    "",
]

ACCEPTED = [
    "SELECT customer_code, net_revenue FROM fact_ai_sales_net",
    "SELECT * FROM dim_b3_item",
    "SELECT COUNT(*) FROM fact_ai_sales_net",
    ("SELECT customer_name, SUM(net_revenue) AS total FROM fact_ai_sales_net "
    "GROUP BY customer_name ORDER BY total DESC"),
    ("SELECT warehouse_name, AVG(net_revenue) AS avg_rev FROM fact_ai_sales_net "
    "WHERE year = '2025' GROUP BY warehouse_name"),
    ("WITH top AS (SELECT customer_code, SUM(net_revenue) AS rev "
    "FROM fact_ai_sales_net GROUP BY customer_code) "
    "SELECT customer_code, rev FROM top ORDER BY rev DESC"),
    ("SELECT f.item_code, d.item_name, f.net_revenue FROM fact_ai_sales_net f "
    "JOIN dim_b3_item d ON f.item_code = d.item_code"),
    "SELECT item_code FROM dim_b3_item UNION SELECT item_code FROM engine_margin_base",
    "SELECT COALESCE(SUM(net_revenue), 0) AS rev FROM fact_ai_sales_net",
    ("SELECT strftime('%Y', posting_date_iso) AS yr, SUM(net_revenue) AS rev "
    "FROM fact_ai_sales_net GROUP BY yr"),
    "SELECT customer_code FROM fact_ai_sales_net LIMIT 10",
    "SELECT t.item_code FROM (SELECT item_code FROM dim_b3_item) t",
    "SELECT item_code, margin_pct FROM engine_margin_base WHERE margin_pct > 0.3",
    ("SELECT customer_code, net_revenue FROM fact_ai_sales_net "
    "WHERE net_revenue > (SELECT AVG(net_revenue) FROM fact_ai_sales_net)"),
    ("SELECT year_month, SUM(net_quantity) AS qty FROM fact_ai_sales_net "
    "GROUP BY year_month HAVING qty > 0"),
]


@pytest.mark.parametrize("sql", REJECTED)
def test_rejected(sql):
    with pytest.raises(SQLValidationError):
        validate_sql(sql, WHITELIST, max_rows=500)


@pytest.mark.parametrize("sql", ACCEPTED)
def test_accepted(sql):
    validated = validate_sql(sql, WHITELIST, max_rows=500)
    assert validated.sql
    assert "LIMIT" in validated.sql.upper()


def test_limit_injected_when_missing():
    v = validate_sql("SELECT customer_code FROM fact_ai_sales_net", WHITELIST, 500)
    assert "LIMIT 500" in v.sql


def test_small_limit_preserved():
    v = validate_sql("SELECT customer_code FROM fact_ai_sales_net LIMIT 10", WHITELIST, 500)
    assert "LIMIT 10" in v.sql


def test_oversized_limit_reduced():
    v = validate_sql("SELECT customer_code FROM fact_ai_sales_net LIMIT 99999", WHITELIST, 500)
    assert "LIMIT 500" in v.sql


def test_referenced_tables_reported():
    v = validate_sql(
        "SELECT f.item_code FROM fact_ai_sales_net f JOIN dim_b3_item d "
        "ON f.item_code = d.item_code",
        WHITELIST, 500,
    )
    assert set(v.tables) == {"fact_ai_sales_net", "dim_b3_item"}


def test_sql_is_regenerated_not_echoed():
    v = validate_sql("select    customer_code   from fact_ai_sales_net", WHITELIST, 500)
    assert "    " not in v.sql


def test_tokenizer_errors_become_validation_errors():
    """Regression: prose with an apostrophe raised TokenError past the net."""
    prose = "The tables needed to calculate margin don't exist in this schema"
    with pytest.raises(SQLValidationError):
        validate_sql(prose, WHITELIST, 500)


def test_unterminated_string_rejected_cleanly():
    with pytest.raises(SQLValidationError):
        validate_sql("SELECT 'unterminated FROM fact_ai_sales_net", WHITELIST, 500)


def test_noop_queries_rejected():
    """Regression: Haiku punted with 'SELECT NULL LIMIT 0' — accepted, then the
    empty result was business-misinterpreted downstream. No-table queries are
    analytically useless and must be rejected so the corrective retry fires."""
    for sql in ("SELECT NULL", "SELECT 1", "SELECT NULL LIMIT 0", "SELECT 1+1 AS x"):
        with pytest.raises(SQLValidationError):
            validate_sql(sql, WHITELIST, 500)


def test_full_outer_join_rejection_reason_is_actionable():
    """The golden list above proves it's rejected; this proves the reason is
    specific enough for the corrective retry to act on, not just "rejected"."""
    with pytest.raises(SQLValidationError) as exc_info:
        validate_sql(
            "SELECT f.customer_code FROM fact_ai_sales_net f "
            "FULL OUTER JOIN dim_b3_item d ON f.item_code = d.item_code",
            WHITELIST, 500,
        )
    assert "FULL" in exc_info.value.internal_detail
    assert "LEFT JOIN" in exc_info.value.internal_detail


def test_right_join_rejection_reason_is_actionable():
    with pytest.raises(SQLValidationError) as exc_info:
        validate_sql(
            "SELECT f.customer_code FROM fact_ai_sales_net f "
            "RIGHT JOIN dim_b3_item d ON f.item_code = d.item_code",
            WHITELIST, 500,
        )
    assert "RIGHT" in exc_info.value.internal_detail


def test_left_join_still_accepted():
    """Regression guard: the FULL/RIGHT rejection must not catch plain and
    LEFT joins, which are portable and already covered in ACCEPTED above —
    this pins that a swapped table order (the suggested RIGHT JOIN fix)
    actually works."""
    validate_sql(
        "SELECT f.customer_code, d.item_name FROM dim_b3_item d "
        "LEFT JOIN fact_ai_sales_net f ON f.item_code = d.item_code",
        WHITELIST, 500,
    )


# ---------------------------------------------- schema slice carries samples


def test_schema_slice_carries_sample_values(fixture_db):
    """Root cause of a real "no data found" failure: the slice gave the SQL
    generator only column NAMES, so it had to guess whether `year_month`
    held "202604" or "2026-04". It guessed wrong, got zero rows, and the
    answer reported that no Q2 margin data existed — a confident wrong
    answer, not a visible error."""
    from app.discovery.index import MetadataIndex
    from app.discovery.metadata_loader import MetadataLoader
    from app.execution.executor import ReadOnlyExecutor
    from app.sqlgen.schema_context import build_slice

    executor = ReadOnlyExecutor(fixture_db, query_timeout_s=5, max_rows=500)
    try:
        objects, registry, glossary, fingerprint = MetadataLoader(executor).load()
        index = MetadataIndex(objects, registry, glossary, fingerprint)
        slices = build_slice(index, ["fact_ai_sales_net"], [])

        samples = slices[0].samples
        assert samples, "no sample values captured"
        # the two date columns encode the SAME day in DIFFERENT formats —
        # exactly the ambiguity a generator cannot resolve from names alone
        assert samples["posting_date_iso"] == "2020-10-01"
        assert samples["posting_date"] != samples["posting_date_iso"]
        # every sampled column is a real column of that object
        assert set(samples).issubset(set(slices[0].columns))
    finally:
        executor.close()


def test_sample_capture_survives_an_empty_object(tmp_path):
    """Samples are a nicety — an object with no rows must yield no samples
    rather than breaking discovery for every other object."""
    import sqlite3

    from app.discovery.metadata_loader import MetadataLoader
    from app.execution.executor import ReadOnlyExecutor

    path = tmp_path / "empty.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE fact_ai_sales_net(customer_code TEXT, net_revenue REAL)")
    conn.commit()
    conn.close()

    executor = ReadOnlyExecutor(path, query_timeout_s=5, max_rows=500)
    try:
        objects, _, _, _ = MetadataLoader(executor).load()
        assert objects["fact_ai_sales_net"].samples == {}
        assert objects["fact_ai_sales_net"].columns  # discovery still worked
    finally:
        executor.close()


def test_a_broken_view_is_skipped_not_a_startup_crash(tmp_path, capsys):
    """A view whose SELECT references a column a base table no longer has
    used to crash pragma_table_info UNCAUGHT — since this runs at app
    startup, one stale view anywhere in the whole database took the entire
    platform down. Now it's excluded from the catalogue and everything else
    still loads."""
    import sqlite3

    from app.discovery.metadata_loader import MetadataLoader
    from app.execution.executor import ReadOnlyExecutor

    path = tmp_path / "broken_view.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE dim_b3_item(item_code TEXT)")
    conn.execute("CREATE VIEW vw_broken AS SELECT nonexistent_column FROM dim_b3_item")
    conn.execute("CREATE VIEW vw_healthy AS SELECT item_code FROM dim_b3_item")
    conn.commit()
    conn.close()

    executor = ReadOnlyExecutor(path, query_timeout_s=5, max_rows=500)
    try:
        objects, _, _, _ = MetadataLoader(executor).load()
        log_output = capsys.readouterr().out

        assert "vw_broken" not in objects  # excluded, not crashed
        assert "vw_healthy" in objects  # everything else still loaded
        assert "object_metadata_unavailable" in log_output
        assert "vw_broken" in log_output
        # renderer-agnostic: plain key=value in isolation, JSON in the full
        # suite (matches production's actual log format) — "reason" plus the
        # real exception text must survive either way
        assert "reason" in log_output
        assert "no such column" in log_output
    finally:
        executor.close()


def test_sample_row_unavailable_reason_is_logged(tmp_path, capsys):
    """A view whose schema is valid (pragma_table_info succeeds — so it's
    NOT excluded from the catalogue) but whose SELECT times out at
    execution — the samples-specific best-effort path, distinct from the
    schema-resolution failure above. Discovery still completes; the object
    still gets a full column list, just no sample values."""
    import sqlite3

    from app.discovery.metadata_loader import MetadataLoader
    from app.execution.executor import ReadOnlyExecutor

    path = tmp_path / "slow_view.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE dim_b3_item(item_code TEXT)")
    # pragma_table_info resolves this view's single output column without
    # running the recursive body, but SELECT * actually executes it — with
    # a high enough bound, this blows well past a short query timeout.
    # SUM forces the recursive set to fully materialize before producing
    # its one output row — unlike a bare SELECT, this can't short-circuit via
    # LIMIT 1 and lazily return the first row before the interrupt fires.
    conn.execute(
        "CREATE VIEW vw_ai_top_items AS "
        "WITH RECURSIVE cnt(x) AS "
        "(SELECT 1 UNION ALL SELECT x+1 FROM cnt WHERE x < 50000000) SELECT SUM(x) AS total FROM cnt"
    )
    conn.commit()
    conn.close()

    executor = ReadOnlyExecutor(path, query_timeout_s=0.01, max_rows=500)
    try:
        objects, _, _, _ = MetadataLoader(executor).load()
        log_output = capsys.readouterr().out

        assert "vw_ai_top_items" in objects  # schema-level info still available
        assert objects["vw_ai_top_items"].samples == {}
        assert "sample_row_unavailable" in log_output
        assert "vw_ai_top_items" in log_output
        assert "reason" in log_output
    finally:
        executor.close()
