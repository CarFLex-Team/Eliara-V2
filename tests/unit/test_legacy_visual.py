"""_build_visual: trend detection, and the boundary it deliberately does NOT
cross into distribution.

The property under test for trend: a 2-column (calendar-unit, number) result
becomes a line, never a bar-chart ranking, regardless of row count — because
"revenue by month" read as a leaderboard is misleading even when there
happen to be few enough months to fit the ranking row cap.

The property under test for the boundary: a small, complete, all-positive
"top N" result — the exact shape a naive distribution heuristic would grab —
must still come back as ranking. That's the regression this file protects:
an earlier version of this function used `not result.truncated` as a proxy
for "this is the whole universe, not a slice", which is wrong — `truncated`
only reflects the EXECUTOR's hard row cap, not a view's own `TOP N` in its
SQL, so a curated "top 5" view is indistinguishable from a genuine breakdown
by shape alone.
"""

from app.core.models import QueryResult
from app.execution.visual import build_visual as _build_visual


def _result(columns, rows, truncated=False, source="view", object_name="vw_test") -> QueryResult:
    return QueryResult(
        columns=columns, rows=rows, row_count=len(rows), truncated=truncated,
        source=source, object_name=object_name, elapsed_ms=5,
    )


# ------------------------------------------------------------------- trend


def test_year_month_column_becomes_a_trend_not_a_ranking():
    result = _result(
        ["year_month", "net_revenue"],
        [("2026-03", 100.0), ("2026-01", 300.0), ("2026-02", 200.0)],
    )
    visual = _build_visual(result, "vw_ai_sales_by_month")

    assert visual["type"] == "trend"
    assert visual["trend"] == [
        {"label": "2026-01", "value": 300.0},
        {"label": "2026-02", "value": 200.0},
        {"label": "2026-03", "value": 100.0},
    ]


def test_trend_points_are_sorted_chronologically_even_if_the_query_was_not():
    """Guards against a view returning rows in insertion order rather than
    date order — the chart would otherwise zigzag."""
    result = _result(["quarter", "revenue"], [("Q4", 40.0), ("Q1", 10.0), ("Q3", 30.0), ("Q2", 20.0)])
    visual = _build_visual(result, "vw_quarterly")

    labels = [p["label"] for p in visual["trend"]]
    assert labels == ["Q1", "Q2", "Q3", "Q4"]


def test_trend_fires_regardless_of_row_count_unlike_ranking():
    """12 months exceeds nothing here — the old code would have sent this
    through the ranking path (which happens to allow up to 15) and rendered
    it as a bar-chart leaderboard of months, which is not what "revenue by
    month" means."""
    rows = [(f"2026-{m:02d}", float(m * 100)) for m in range(1, 13)]
    result = _result(["month", "revenue"], rows)
    visual = _build_visual(result, "vw_monthly")

    assert visual["type"] == "trend"
    assert len(visual["trend"]) == 12


def test_date_column_name_also_matches():
    result = _result(["order_date", "count"], [("2026-01-15", 5.0), ("2026-01-16", 3.0)])
    visual = _build_visual(result, "vw_orders")
    assert visual["type"] == "trend"


# ------------------------------------------------ ranking / distribution boundary


def test_small_complete_positive_top_n_stays_a_ranking_not_a_distribution():
    """This is exactly the shape a naive 'not truncated + few positive rows'
    heuristic would misclassify: a curated top-5 view, not truncated (it
    never hit the executor's row cap), every value positive. It must render
    as ranking, not distribution — see module docstring."""
    result = _result(
        ["customer_name", "lifetime_revenue"],
        [("Alpha", 500.0), ("Beta", 400.0), ("Gamma", 300.0), ("Delta", 200.0), ("Epsilon", 100.0)],
        truncated=False,
    )
    visual = _build_visual(result, "vw_q_top_5_customers")

    assert visual["type"] == "ranking"
    assert len(visual["ranking"]) == 5


def test_distribution_type_is_never_emitted():
    """No shape should ever produce distribution — it was deliberately left
    unimplemented pending a real intent signal, not a shape guess."""
    shapes = [
        _result(["region", "share"], [("North", 30.0), ("South", 25.0), ("East", 45.0)]),
        _result(["category", "count"], [("A", 1.0), ("B", 2.0)]),
        _result(["warehouse", "pct"], [(f"WH{i}", float(i)) for i in range(1, 6)]),
    ]
    for result in shapes:
        visual = _build_visual(result, "vw_something")
        assert visual["type"] != "distribution"


# ------------------------------------------- trend with realistic extra columns


def test_trend_fires_with_auxiliary_date_range_columns_alongside_the_period():
    """Exact shape from a real production report: a monthly view returning
    year_month, a period_start/period_end date range, AND two metrics
    (net_revenue, net_gross_profit). The old 2-column-only rule fell straight
    through to 'table' here — this is the regression it was widened for."""
    result = _result(
        ["year_month", "period_start", "period_end", "net_revenue", "net_gross_profit"],
        [
            ("2025-07", "2025-07-01", "2025-07-31", 45000.0, 12000.0),
            ("2025-08", "2025-08-01", "2025-08-31", 52000.0, 14000.0),
            ("2025-06", "2025-06-01", "2025-06-30", 38000.0, 9000.0),
        ],
    )
    visual = _build_visual(result, "vw_revenue_by_month")

    assert visual["type"] == "trend"
    assert [p["label"] for p in visual["trend"]] == ["2025-06", "2025-07", "2025-08"]
    # the FIRST qualifying metric (net_revenue) is charted, not net_gross_profit
    assert visual["trend"][0]["value"] == 38000.0


def test_trend_skips_a_second_period_like_column_when_picking_the_metric():
    """period_start and period_end are both period-like by name but neither
    is the value — the metric must be the first NUMERIC, non-period column,
    never one of the date-range columns even though they're technically
    numeric-adjacent in meaning."""
    result = _result(
        ["month", "period_start", "revenue"],
        [("2025-01", "2025-01-01", 100.0), ("2025-02", "2025-02-01", 200.0)],
    )
    visual = _build_visual(result, "vw_test")

    assert visual["type"] == "trend"
    assert visual["trend"][0]["value"] in {100.0, 200.0}  # picked "revenue", not the date


def test_trend_still_fires_for_the_plain_two_column_case():
    """Regression guard: the original narrow case must keep working exactly
    as before the widening."""
    result = _result(["year_month", "net_revenue"], [("2025-01", 100.0), ("2025-02", 200.0)])
    visual = _build_visual(result, "vw_test")
    assert visual["type"] == "trend"


def test_no_metric_column_falls_through_to_table_not_a_broken_trend():
    """A period-first result with no numeric column at all (every column is
    an id/code/text) has nothing to chart — must land on table, not crash or
    fabricate a trend with no value."""
    result = _result(
        ["year_month", "customer_code", "customer_name"],
        [("2025-01", "C001", "Alpha"), ("2025-02", "C002", "Beta")],
    )
    visual = _build_visual(result, "vw_test")
    assert visual["type"] == "table"


# ---------------------------------------------------------- existing behaviour


def test_ranking_unaffected_for_non_period_two_column_results():
    result = _result(["item_name", "qty"], [("Bolt", 10.0), ("Nut", 5.0)])
    visual = _build_visual(result, "vw_items")
    assert visual["type"] == "ranking"


def test_table_unaffected_for_multi_column_results():
    result = _result(
        ["customer_name", "revenue", "margin_pct"],
        [("Alpha", 100.0, 0.2)],
    )
    visual = _build_visual(result, "vw_wide")
    assert visual["type"] == "table"


def test_period_name_with_non_numeric_second_column_falls_through_to_table():
    """A period-looking first column doesn't force trend if the data isn't
    actually numeric — trend requires BOTH conditions."""
    result = _result(["month", "top_customer"], [("2026-01", "Alpha"), ("2026-02", "Beta")])
    visual = _build_visual(result, "vw_monthly_leader")
    assert visual["type"] == "table"


def test_single_row_comparison_is_a_table_not_a_one_point_trend():
    """Real production case: "actual sales of feb 2026 vs historical avg"
    generated SQL returning ONE row whose first column is a synthetic label
    literally named `period`, with the two values being compared sitting in
    SEPARATE COLUMNS. That rendered as a line chart with a single dot, which
    silently dropped the historical average entirely — the whole point of the
    question. A table shows both figures side by side."""
    result = _result(
        ["period", "feb_2026_revenue", "historical_avg_monthly_revenue",
         "revenue_variance_absolute", "revenue_variance_pct"],
        [("February 2026", 3565921.63, 2786300.0, 779621.63, 27.98)],
    )
    visual = _build_visual(result, None)

    assert visual["type"] == "table"
    # both compared figures survive into the rendering
    assert any("3,565,921" in cell for cell in visual["rows"][0])
    assert any("2,786,300" in cell for cell in visual["rows"][0])


def test_two_point_trend_still_fires():
    """The minimum is 2, not 3 — a two-month comparison IS a direction."""
    result = _result(["year_month", "net_revenue"], [("2026-01", 100.0), ("2026-02", 200.0)])
    assert _build_visual(result, None)["type"] == "trend"
