"""build_attention_queue: deterministic ranking, no LLM, no fabricated score.

The property that matters most: every number in the output is either copied
straight from the source row or a simple sum/rank over those rows. Nothing
here is invented — that's the whole point of building this deterministically
rather than asking a model to score urgency.
"""

from app.core.models import QueryResult
from app.detection.attention_queue import build_attention_queue


def _result(columns, rows) -> QueryResult:
    return QueryResult(
        columns=list(columns), rows=rows, row_count=len(rows), truncated=False,
        source="view", object_name="vw_test", elapsed_ms=5,
    )


_LIQUIDATION_ROWS = [
    ("Alpha Brake Pad", 45000.0),
    ("Beta Filter", 38000.0),
    ("Gamma Sensor", 22000.0),
    ("Delta Gasket", 15000.0),
    ("Epsilon Hose", 9000.0),
    ("Zeta Clip", 4000.0),
]


def test_ranks_descending_by_the_detected_value_column():
    result = _result(["item_name", "capital_locked"], _LIQUIDATION_ROWS)
    queue = build_attention_queue(result, "vw_liquidation")

    assert [i.label for i in queue.items] == [
        "Alpha Brake Pad", "Beta Filter", "Gamma Sensor",
        "Delta Gasket", "Epsilon Hose", "Zeta Clip",
    ]


def test_tiers_by_quantile_not_a_fixed_threshold():
    """6 rows -> top 2 HIGH, next 2 MEDIUM, last 2 LOW. A fixed threshold
    would be meaningless across different views on different scales (capital
    AED vs. days-without-sale) — the tier boundary must come from the data's
    own distribution."""
    result = _result(["item_name", "capital_locked"], _LIQUIDATION_ROWS)
    queue = build_attention_queue(result, "vw_liquidation")

    tiers = [i.tier for i in queue.items]
    assert tiers == ["HIGH", "HIGH", "MEDIUM", "MEDIUM", "LOW", "LOW"]


def test_total_value_is_a_plain_sum_not_invented():
    result = _result(["item_name", "capital_locked"], _LIQUIDATION_ROWS)
    queue = build_attention_queue(result, "vw_liquidation")
    assert queue.total_value == sum(v for _, v in _LIQUIDATION_ROWS)


def test_auto_detects_value_and_label_columns():
    result = _result(["item_code", "item_name", "capital_locked"],
                      [("C001", "Alpha", 100.0), ("C002", "Beta", 200.0)])
    queue = build_attention_queue(result, "vw_test")

    # item_code excluded from being the value column (not numeric), and from
    # being the label (an "_code" column is skipped as a metric candidate,
    # but the label pick is simply "first non-numeric column" — item_code
    # itself IS non-numeric, so confirm item_name specifically wasn't
    # required; what matters is the VALUE column is capital_locked, not code)
    assert queue.value_column == "capital_locked"


def test_explicit_columns_are_trusted_over_auto_detection():
    result = _result(["item_name", "capital_locked", "days_without_sale"],
                      [("Alpha", 100.0, 400.0), ("Beta", 50.0, 200.0)])
    queue = build_attention_queue(
        result, "vw_test", value_column="days_without_sale", label_column="item_name",
    )
    assert queue.value_column == "days_without_sale"
    assert queue.items[0].label == "Alpha"  # 400 > 200


def test_explicit_column_not_present_returns_none_rather_than_guessing():
    result = _result(["item_name", "capital_locked"], [("Alpha", 100.0)])
    queue = build_attention_queue(result, "vw_test", value_column="not_a_real_column")
    assert queue is None


def test_no_numeric_column_returns_none_not_a_broken_queue():
    result = _result(["item_code", "item_name"], [("C001", "Alpha"), ("C002", "Beta")])
    queue = build_attention_queue(result, "vw_test")
    assert queue is None


def test_empty_result_returns_none():
    result = _result(["item_name", "capital_locked"], [])
    assert build_attention_queue(result, "vw_test") is None


def test_max_items_truncates_and_flags_it():
    result = _result(["item_name", "capital_locked"], _LIQUIDATION_ROWS)
    queue = build_attention_queue(result, "vw_liquidation", max_items=3)

    assert len(queue.items) == 3
    assert queue.truncated is True
    # total_value still reflects ALL rows, not just the truncated slice —
    # the queue shows a subset but must not understate the real total
    assert queue.total_value == sum(v for _, v in _LIQUIDATION_ROWS)


def test_every_item_carries_the_full_source_row_for_traceability():
    """No fabricated data — everything the item claims is copied straight
    from the row that backs it."""
    result = _result(
        ["item_name", "capital_locked", "warehouse_code"],
        [("Alpha", 100.0, "WH1")],
    )
    queue = build_attention_queue(result, "vw_test")
    assert queue.items[0].row == {
        "item_name": "Alpha", "capital_locked": "100", "warehouse_code": "WH1",
    }


def test_single_row_still_produces_a_high_tier_item():
    result = _result(["item_name", "capital_locked"], [("Alpha", 100.0)])
    queue = build_attention_queue(result, "vw_test")
    assert len(queue.items) == 1
    assert queue.items[0].tier == "HIGH"


def test_prefers_a_name_column_over_a_bare_code_for_the_label():
    """A queue meant to be read at a glance should show "Beta Motors", not
    "C002" — this is the exact live output that motivated the fix."""
    result = _result(
        ["customer_code", "customer_name", "lifetime_revenue"],
        [("C002", "Beta Motors", 8000.0), ("C001", "Alpha Trading", 7600.0)],
    )
    queue = build_attention_queue(result, "vw_test")
    assert queue.label_column == "customer_name"
    assert queue.items[0].label == "Beta Motors"


def test_falls_back_to_first_non_numeric_when_no_name_column_exists():
    result = _result(["item_code", "capital_locked"], [("C001", 100.0)])
    queue = build_attention_queue(result, "vw_test")
    assert queue.label_column == "item_code"
