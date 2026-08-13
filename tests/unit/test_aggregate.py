"""Aggregation tests, anchored on the production deadstock failure.

The system was asked "talk me about our deadstock", got 500 rows, showed 13,
and answered: "the total capital locked is likely substantially higher than
what is visible here." It could not add up its own result set.
"""

from app.core.models import QueryResult
from app.execution.aggregate import summarise


def _result(rows, columns, truncated=False):
    return QueryResult(
        columns=columns,
        rows=rows,
        row_count=len(rows),
        truncated=truncated,
        source="view",
        object_name="vw_test",
        elapsed_ms=1,
    )


def _deadstock(n=500):
    """Shaped like vw_q011: capital locked per SKU across item groups."""
    groups = ["SKODA", "AUDI-A4", "BMW-X5-G05", "PORSCHE-CAYENNE", "VW-GOLF"]
    return _result(
        [(f"ITEM-{i:04d}", groups[i % len(groups)], 100.0 + i) for i in range(n)],
        ["item_code", "item_group_name", "capital_locked"],
    )


def test_totals_cover_every_row_not_just_the_sample():
    """The fix for the deadstock answer: 500 rows in, 13 shown, but the total
    is computed over all 500."""
    result = _deadstock(500)
    stats = summarise(result, rows_shown=13)

    expected_total = sum(100.0 + i for i in range(500))
    assert f"{expected_total:,.0f}" in stats
    assert "500 total" in stats
    assert "13 shown above" in stats
    assert "487 not shown" in stats
    assert "cover ALL 500 rows" in stats


def test_group_breakdown_is_present():
    stats = summarise(_deadstock(500), rows_shown=13)
    assert "capital_locked by item_group_name" in stats
    assert "SKODA" in stats


def test_concentration_is_reported():
    stats = summarise(_deadstock(500), rows_shown=13)
    assert "Concentration" in stats
    assert "top 10 rows" in stats


def test_row_cap_is_flagged_so_the_model_can_still_hedge():
    """When the query itself was capped, the totals are a floor — say so."""
    result = _deadstock(500)
    result.truncated = True
    stats = summarise(result, rows_shown=13)
    assert "row cap" in stats
    assert "more rows exist" in stats


def test_no_stats_for_tiny_result_sets():
    """With three rows the model sees everything; stats would be noise."""
    assert summarise(_result([(1, 2.0), (2, 3.0)], ["a", "b"]), rows_shown=2) is None


def test_no_stats_for_empty_results():
    assert summarise(_result([], ["a"]), rows_shown=0) is None


def test_identifier_columns_are_not_summed():
    """Summing customer_code or year produces meaningless numbers."""
    rows = [(2024, f"C{i:05d}", 100.0) for i in range(20)]
    stats = summarise(_result(rows, ["year", "customer_code", "revenue"]), rows_shown=5)
    assert "revenue: total" in stats
    assert "year: total" not in stats
    assert "customer_code: total" not in stats


def test_high_cardinality_columns_are_not_used_as_groups():
    """item_code has 500 distinct values — a breakdown by it is a table, not
    a summary."""
    rows = [(f"ITEM-{i}", 10.0) for i in range(100)]
    stats = summarise(_result(rows, ["item_code", "capital_locked"]), rows_shown=5)
    assert "by item_code" not in stats


def test_nulls_do_not_break_the_summary():
    rows = [(f"I{i}", None if i % 3 == 0 else float(i)) for i in range(30)]
    stats = summarise(_result(rows, ["item_code", "value"]), rows_shown=5)
    assert stats is not None
    assert "value: total" in stats


def test_all_shown_states_so_plainly():
    rows = [(f"I{i}", float(i)) for i in range(10)]
    stats = summarise(_result(rows, ["item", "v"]), rows_shown=10)
    assert "all shown above" in stats
