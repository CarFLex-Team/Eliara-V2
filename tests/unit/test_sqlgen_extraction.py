from app.sqlgen.generator import _extract_sql


def test_fenced_sql_extracted():
    text = "Here is the query:\n```sql\nSELECT a FROM t\n```\nHope this helps!"
    assert _extract_sql(text) == "SELECT a FROM t"


def test_prose_wrapped_sql_extracted():
    text = (
        "-- Tables needed to calculate gross margin percentage\n"
        "To answer this, we don't need dead stock rows.\n"
        "WITH base AS (SELECT item_code FROM fact) SELECT item_code FROM base"
    )
    extracted = _extract_sql(text)
    assert extracted.startswith("WITH base")


def test_pure_prose_returned_for_validator_to_reject():
    text = "I cannot answer this question."
    assert _extract_sql(text) == text


def test_plain_sql_untouched():
    assert _extract_sql("SELECT 1") == "SELECT 1"
