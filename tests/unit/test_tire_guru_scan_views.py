"""Regression tests for app.detection.attention_queue's auto-column-detection
against Tire Guru's real scan-view shapes (issue #6).

While wiring up Tire Guru's /scan configuration, a real ranking bug was
caught before shipping: querying the raw chatbot_dead_stock_fast table
(29 columns, SELECT * semantics) auto-detects on_hand_quantity as the
"value" column — the first numeric column not matching the non-metric
hints (_id, _code, rank, score, _pct, ...) — rather than a deadness
measure like days_since_last_sale or dead_stock_score. That ranks items
with lots of stock as HIGH attention and genuinely dead items as LOW —
backwards.

The fix was NOT a code change to the detector (its column-order-based
heuristic is working exactly as documented) — it was curating each scan
view's SELECT column list and order so the correct column gets picked.
These tests pin that curation against the real column shapes so a future
edit to any of the four Tire Guru scan views can't silently reintroduce
the same backwards-ranking bug.
"""

from app.core.models import QueryResult
from app.detection.attention_queue import build_attention_queue


def _result(name, columns, rows):
    return QueryResult(
        object_name=name, columns=columns, rows=rows, row_count=len(rows),
        truncated=False, source="view", elapsed_ms=1,
    )


def test_raw_dead_stock_table_would_rank_backwards_by_on_hand_quantity():
    """Documents the exact bug that was caught: querying the raw table
    (SELECT * shape) picks on_hand_quantity as the value column, not a
    deadness measure. This is intentionally a negative test — it exists so
    the danger stays visible, not so anything here gets "fixed" at the
    detector level (the fix belongs in view curation, per the module
    above)."""
    columns = [
        "external_item_id", "canonical_size", "brand", "model", "description",
        "warehouse_code", "on_hand_quantity", "committed_quantity", "hold_quantity",
        "damaged_quantity", "warranty_quantity", "quarantine_quantity",
        "raw_available_quantity", "available_quantity", "on_order_quantity",
        "projected_quantity", "valuation_unit_cost", "valuation_cost_source",
        "current_price", "dealer_price", "public_price", "current_unit_margin_amount",
        "current_unit_margin_pct", "last_sale_date", "days_since_last_sale",
        "inventory_health_status", "dead_stock_score", "currency_code", "analysis_date",
    ]

    def row(on_hand, days, score):
        return (
            "IT1", "195/65R15", "BrandX", "ModelY", "desc", "WH1", on_hand, 0, 0, 0, 0, 0,
            on_hand, on_hand, 0, 0, 50.0, "cost", "60", "55", "65", 5.0, 10.0,
            "2026-01-01", days, "DEAD", score, "AED", "2026-08-18",
        )

    # barely-dead item with lots of stock vs. genuinely-dead item with almost none
    rows = [row(500, 10, 20), row(5, 400, 95)]
    result = _result("chatbot_dead_stock_fast", columns, rows)
    q = build_attention_queue(result, "chatbot_dead_stock_fast", max_items=10)

    assert q.value_column == "on_hand_quantity"  # the bug, pinned so it stays documented
    high = next(i for i in q.items if i.tier == "HIGH")
    assert high.value == 500.0  # the barely-dead item — backwards ranking


def test_curated_dead_stock_view_ranks_by_days_since_last_sale():
    columns = ["item_label", "days_since_last_sale", "dead_stock_score", "on_hand_quantity",
               "brand", "canonical_size", "warehouse_code", "inventory_health_status"]
    rows = [
        ("ModelY", 400, 95, 5, "BrandX", "195/65R15", "WH1", "DEAD_STOCK"),
        ("ModelZ", 10, 20, 500, "BrandX", "195/65R15", "WH1", "ACTIVE_STOCK"),
    ]
    result = _result("vw_tire_guru_dead_stock_ranked", columns, rows)
    q = build_attention_queue(result, "vw_tire_guru_dead_stock_ranked", max_items=10)

    assert q.value_column == "days_since_last_sale"
    assert q.label_column == "item_label"
    high = next(i for i in q.items if i.tier == "HIGH")
    assert high.label == "ModelY"  # the genuinely-dead item now correctly ranks HIGH


def test_curated_slow_moving_view_ranks_by_days_since_last_sale():
    columns = ["item_label", "days_since_last_sale", "capital_locked_at_last_cost",
               "stock_classification", "brand", "canonical_size",
               "liquidation_priority_rank", "recommended_action"]
    rows = [
        ("ModelA", 120, 3000.0, "SLOW_MOVING", "BrandX", "205/55R16", 5, "discount"),
        ("ModelB", 200, 8000.0, "SLOW_MOVING", "BrandX", "205/55R16", 2, "discount"),
    ]
    result = _result("vw_tire_guru_slow_moving_items", columns, rows)
    q = build_attention_queue(result, "vw_tire_guru_slow_moving_items", max_items=10)

    assert q.value_column == "days_since_last_sale"
    assert q.label_column == "item_label"


def test_curated_liquidation_view_ranks_by_capital_locked():
    columns = ["item_label", "capital_locked_at_last_cost", "days_since_last_sale",
               "stock_classification", "brand", "canonical_size",
               "liquidation_priority_rank", "recommended_action"]
    rows = [
        ("ModelC", 15000.0, 300, "DEAD_STOCK", "BrandY", "175/70R14", 1, "liquidate"),
        ("ModelD", 2000.0, 250, "DEAD_STOCK", "BrandY", "175/70R14", 2, "liquidate"),
    ]
    result = _result("vw_tire_guru_liquidation_candidates", columns, rows)
    q = build_attention_queue(result, "vw_tire_guru_liquidation_candidates", max_items=10)

    assert q.value_column == "capital_locked_at_last_cost"
    assert q.label_column == "item_label"
    high = next(i for i in q.items if i.tier == "HIGH")
    assert high.label == "ModelC"  # the item with the most capital locked ranks HIGH


def test_curated_critical_stockout_view_ranks_by_recommended_order_quantity():
    columns = ["item_label", "recommended_order_quantity", "days_of_stock_cover",
               "reorder_urgency", "brand", "canonical_size", "sellable_quantity",
               "weighted_average_daily_demand"]
    rows = [
        ("ModelE", 200, 1.0, "CRITICAL_STOCKOUT", "BrandZ", "225/45R17", 0, 15.0),
        ("ModelF", 20, 2.0, "CRITICAL_STOCKOUT", "BrandZ", "225/45R17", 1, 5.0),
    ]
    result = _result("vw_tire_guru_critical_stockout", columns, rows)
    q = build_attention_queue(result, "vw_tire_guru_critical_stockout", max_items=10)

    assert q.value_column == "recommended_order_quantity"
    assert q.label_column == "item_label"
