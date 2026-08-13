"""Regression tests for app.orchestrator.verification.

Covers https://github.com/Mosapmohamd/Eliara-V2/issues/4 — a seasonal
index of 1.17 in the data, correctly described in the narrative as "17%
above baseline," was flagged as an ungrounded (fabricated) figure. The
existing fraction-to-percentage grounding (_rounded_forms) only covers
values in (0, 1] read as a share (0.31 -> "31%"); a value like 1.17 is a
different shape — an index centered on 1.0, written as its deviation from
that center. This file pins the fix (a narrowly-scoped index-deviation
form, 0.5-2.0) against the exact reported false positive, and confirms it
does not also start passing genuinely wrong numbers.

The investigation for this issue also found the *other* originally-recalled
false-positive class (threshold numbers embedded in text-cell labels, e.g.
"239 days") does NOT reproduce in this codebase's verify() — string cells
are already scanned for embedded numbers in _build_ground_truth. No fix was
needed there; a test below pins that this continues to work correctly.
"""

from app.core.models import QueryResult
from app.orchestrator.verification import verify


def _result(rows, columns=("month", "seasonal_index")):
    return QueryResult(
        object_name="vw_test", columns=list(columns), rows=rows,
        row_count=len(rows), truncated=False, source="view", elapsed_ms=1,
    )


def test_seasonal_index_above_baseline_is_grounded():
    """The exact reported false positive: 1.17 in the data, "17% above
    baseline" in the narrative — must now pass, not warn."""
    result = _result([["2026-01", 1.17], ["2026-02", 1.02]])
    report = verify("The seasonal index rose 17% above baseline this month.", result)
    assert report.status == "pass"
    assert report.ungrounded == []


def test_index_below_baseline_written_as_deviation_percent_is_grounded():
    result = _result([["2026-01", 0.82], ["2026-02", 1.0]])
    report = verify("The index came in 18% below baseline.", result)
    assert report.status == "pass"
    assert report.ungrounded == []


def test_plain_fraction_as_percent_still_works_unaffected():
    """The pre-existing fraction->percent grounding (0.31 -> "31%") must
    keep working after adding the index-deviation form."""
    result = _result([["2026-01", 0.31]], columns=("month", "margin_pct"))
    report = verify("Margin was 31% this month.", result)
    assert report.status == "pass"


def test_a_genuinely_fabricated_percentage_is_still_flagged():
    """The fix must not make verification blind to real fabrication —
    an index-shaped false positive fix shouldn't create a new blind spot."""
    result = _result([["2026-01", 1.05], ["2026-02", 0.98]])
    # 273% has no plausible relationship to either row, their total, mean,
    # min, max, or deviation-from-1.0 forms.
    report = verify("Sales are up 273% this quarter.", result)
    assert report.status == "warn"
    assert "273%" in report.ungrounded


def test_index_deviation_form_is_narrowly_scoped_not_applied_far_from_one():
    """The index-deviation transform only applies to values plausibly
    shaped like an index (0.5-2.0) — a value like 8.0 should not start
    grounding an unrelated 700% claim via (8.0-1)*100."""
    result = _result([["2026-01", 8.0]], columns=("month", "multiplier"))
    report = verify("Growth was 700% this month.", result)
    assert report.status == "warn"
    assert "700%" in report.ungrounded


def test_threshold_numbers_embedded_in_row_labels_are_already_grounded():
    """Confirms the *other* originally-reported false-positive class
    (threshold numbers inside text-cell labels) does not reproduce here —
    no fix needed, this already works via embedded-number scanning."""
    result = _result(
        [
            ["Item A (239 days)", 239],
            ["Item B (365 days)", 365],
            ["Item C (181 days)", 181],
        ],
        columns=("item", "days_since_last_sale"),
    )
    report = verify(
        "Item A has been idle for 239 days, Item B for 365 days, "
        "and Item C for 181 days.",
        result,
    )
    assert report.status == "pass"
    assert report.ungrounded == []
