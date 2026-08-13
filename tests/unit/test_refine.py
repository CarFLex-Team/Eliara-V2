"""apply_refinement: pure-Python sort/filter/limit over an in-memory result.

The property that matters most: an unknown column is a hard rejection, never
a silent no-op. A refinement that quietly ignored a bad column would answer
a DIFFERENT question than the one asked and look correct.
"""

import pytest

from app.core.models import QueryResult
from app.execution.refine import RefineError, RefineFilter, RefineSpec, apply_refinement


def _result(rows, columns=("customer_name", "revenue", "margin_pct")) -> QueryResult:
    return QueryResult(
        columns=list(columns), rows=rows, row_count=len(rows), truncated=False,
        source="view", object_name="vw_test", elapsed_ms=0,
    )


_ROWS = [
    ("Alpha Trading", 7_600.0, 0.31),
    ("MERSIN TRADE", 5_674_262.0, 0.20),
    ("HALA CAR CO", 4_469_733.0, 0.31),
    ("No Margin Co", 1_200.0, None),
]


def test_sort_descending_by_a_numeric_column():
    out = apply_refinement(_result(_ROWS), RefineSpec(sort_by="revenue", sort_desc=True))
    assert [r[0] for r in out.rows] == [
        "MERSIN TRADE", "HALA CAR CO", "Alpha Trading", "No Margin Co",
    ]


def test_sort_ascending_pushes_nulls_last_either_direction():
    asc = apply_refinement(_result(_ROWS), RefineSpec(sort_by="margin_pct"))
    desc = apply_refinement(_result(_ROWS), RefineSpec(sort_by="margin_pct", sort_desc=True))
    assert asc.rows[-1][0] == "No Margin Co"
    assert desc.rows[-1][0] == "No Margin Co"


def test_filter_equality_coerces_to_the_columns_own_type():
    out = apply_refinement(
        _result(_ROWS),
        RefineSpec(filters=[RefineFilter(column="margin_pct", op="eq", value="0.31")]),
    )
    assert {r[0] for r in out.rows} == {"Alpha Trading", "HALA CAR CO"}


def test_filter_threshold_gt():
    out = apply_refinement(
        _result(_ROWS),
        RefineSpec(filters=[RefineFilter(column="revenue", op="gt", value="5000000")]),
    )
    assert [r[0] for r in out.rows] == ["MERSIN TRADE"]


def test_limit_truncates_and_flags_truncated():
    out = apply_refinement(_result(_ROWS), RefineSpec(sort_by="revenue", sort_desc=True, limit=2))
    assert out.row_count == 4  # count reflects rows AFTER filtering, before the limit
    assert len(out.rows) == 2
    assert out.truncated is True


def test_filter_then_sort_then_limit_compose():
    out = apply_refinement(
        _result(_ROWS),
        RefineSpec(
            filters=[RefineFilter(column="margin_pct", op="gte", value="0.25")],
            sort_by="revenue",
            limit=1,
        ),
    )
    assert len(out.rows) == 1
    assert out.rows[0][0] == "Alpha Trading"  # only 0.31-margin row below HALA in revenue


def test_unknown_sort_column_is_rejected_not_ignored():
    with pytest.raises(RefineError) as exc_info:
        apply_refinement(_result(_ROWS), RefineSpec(sort_by="profit_usd"))
    assert exc_info.value.column == "profit_usd"
    assert "customer_name" in exc_info.value.available


def test_unknown_filter_column_is_rejected_not_ignored():
    with pytest.raises(RefineError):
        apply_refinement(
            _result(_ROWS),
            RefineSpec(filters=[RefineFilter(column="warehouse", op="eq", value="WH1")]),
        )


def test_result_metadata_is_preserved_for_downstream_use():
    out = apply_refinement(_result(_ROWS), RefineSpec(limit=1))
    assert out.source == "view"
    assert out.object_name == "vw_test"
    assert out.elapsed_ms == 0  # no query ran
