from app.execution.executor import ReadOnlyExecutor


def test_run_view_returns_rows(executor):
    result = executor.run_view("vw_q002_top_10_customers_by_lifetime_revenue")
    assert result.source == "view"
    assert result.columns[0] == "customer_code"
    assert result.row_count == 2
    top = result.rows[0]
    assert top[1] == "Beta Motors"  # 8000 > 5000+2600? no: Alpha=7600, Beta=8000
    assert not result.truncated


def test_run_view_with_filter_binds_parameters(executor):
    result = executor.run_view(
        "fact_ai_sales_net", filters={"customer_code": "C001"}
    )
    assert result.row_count == 2
    assert all(r[9] == "C001" for r in result.rows)


def test_row_cap_truncates(fixture_db, tmp_path):
    from tests.fixtures.fixture_db import build_fixture_db

    db = build_fixture_db(tmp_path / "big.db", extra_sales_rows=100)
    ex = ReadOnlyExecutor(db, max_rows=50)
    try:
        result = ex.run_view("fact_ai_sales_net")
        assert result.row_count == 50
        assert result.truncated
    finally:
        ex.close()


def test_limit_is_capped_at_max_rows(executor):
    result = executor.run_view("fact_ai_sales_net", limit=10_000)
    assert result.row_count <= 500


def test_data_boundaries(executor):
    boundaries = executor.data_boundaries(
        table="fact_ai_sales_net", date_column="posting_date_iso"
    )
    assert boundaries is not None
    assert boundaries.first_date == "2020-10-01"
    assert boundaries.last_date == "2026-06-27"


def test_data_boundaries_none_when_not_configured(executor):
    assert executor.data_boundaries() is None
    assert executor.data_boundaries(table="fact_ai_sales_net") is None


def test_registry_readable(executor):
    result = executor.run_view("chatbot_question_view_registry")
    assert result.row_count == 3
    assert "canonical_question" in result.columns
