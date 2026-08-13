"""Turns a curated view's rows into a ranked attention queue.

The gap this closes: the platform already has real, human-approved views for
exactly the kind of thing an owner needs surfaced without asking —
liquidation candidates, slow-moving stock, replenishment shortfalls (see
app/orchestrator/definitions/stock_action_plan.yaml). But nothing ranks them
or watches them; someone has to think to ask "what's my stock action plan"
before any of it is seen. This module is the deterministic core of a
detector: given a view's result, rank it and tier it into HIGH/MEDIUM/LOW,
with every number traced back to the view it came from.

Deliberately NOT here: a confidence score, a financial-impact formula, an
LLM-written urgency judgment. Every one of those would be a number this
system cannot yet back up — this session's own history is a list of
plausible-looking numbers that turned out to be a format guess or a
misrouted query. A detector's credibility depends on never doing that. The
ranking below is sort-by-the-view's-own-column-and-quantile-tier: fully
inspectable, nothing invented.

Also deliberately NOT here: assumed real column names for the actual
production views. Only the fixture's minimal stub of vw_q011 has ever been
seen in this codebase; the real dead-stock/liquidation views' actual columns
are unconfirmed. So detection of which column is the "value" and which is
the "label" is done the same way trend/ranking chart detection already
works in app/execution/visual.py — by shape and naming convention, checked
against whatever the view actually returns at runtime, not hard-coded.
"""

from typing import Any

from pydantic import BaseModel

from app.core.models import QueryResult

# Mirrors aggregate.py's _NON_METRIC_HINTS and visual.py's exclusion
# conventions independently rather than importing either — this module's
# concern (which column ranks the queue) is close to but not identical to
# theirs (text-summary stats; chart-shape selection), and a tuning change to
# one should not silently change this one.
_NON_METRIC_HINTS = ("_id", "_code", "rank", "score", "_pct", "percent", "_no", "number")

# Top third by value = HIGH, middle third = MEDIUM, rest = LOW. A quantile
# split rather than a fixed threshold, because "capital locked" and "days
# without a sale" live on completely different scales — a fixed cutoff for
# one is meaningless for the other. Documented here because it is the one
# number in this module that isn't simply "sort by what's in the row".
_HIGH_TIER_FRACTION = 1 / 3
_MEDIUM_TIER_FRACTION = 2 / 3


class AttentionItem(BaseModel):
    label: str
    value: float
    tier: str  # "HIGH" | "MEDIUM" | "LOW"
    row: dict[str, str]  # every column from the source row, as display strings


class AttentionQueue(BaseModel):
    view_name: str
    value_column: str
    label_column: str
    total_value: float
    items: list[AttentionItem]
    truncated: bool  # more rows existed in the view than max_items kept


def _is_numeric(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _looks_like_metric(column: str) -> bool:
    lowered = column.lower()
    return not any(hint in lowered for hint in _NON_METRIC_HINTS)


def _cell(value: Any) -> str:
    if value is None:
        return "—"
    if isinstance(value, float):
        return f"{value:,.2f}" if value % 1 else f"{value:,.0f}"
    if isinstance(value, int):
        return f"{value:,}"
    return str(value).strip() or "—"


def _pick_columns(
    columns: list[str], rows: list[tuple], value_column: str | None, label_column: str | None,
) -> tuple[int, int] | None:
    """Find (label_index, value_index), or None if the shape doesn't work.

    Explicit column names are trusted outright — the caller confirmed them
    against the real view. Auto-detection is the fallback for exploring a
    view whose exact columns haven't been verified yet: the first metric-
    looking numeric column becomes the value, the first non-numeric column
    becomes the label.
    """
    if value_column is not None or label_column is not None:
        if value_column not in columns or label_column not in columns:
            return None
        return columns.index(label_column), columns.index(value_column)

    value_idx = next(
        (i for i, c in enumerate(columns)
         if _looks_like_metric(c) and all(_is_numeric(r[i]) for r in rows)),
        None,
    )
    non_numeric = [i for i, c in enumerate(columns) if not _is_numeric(rows[0][i])]
    # Prefer a "_name"-style column over a bare code/id — a queue meant to be
    # read at a glance should show "Beta Motors", not "C002", when the view
    # offers both. Falls back to the first non-numeric column otherwise.
    label_idx = next(
        (i for i in non_numeric if "name" in columns[i].lower()),
        non_numeric[0] if non_numeric else None,
    )
    if value_idx is None or label_idx is None or value_idx == label_idx:
        return None
    return label_idx, value_idx


def build_attention_queue(
    result: QueryResult,
    view_name: str,
    *,
    value_column: str | None = None,
    label_column: str | None = None,
    max_items: int = 10,
) -> AttentionQueue | None:
    """Rank a curated view's rows into HIGH/MEDIUM/LOW.

    Returns None when the result has no rows or no usable numeric column —
    an empty or unrankable view is a caller decision (skip it, log it), not
    this function's to paper over with a fabricated queue.
    """
    if not result.rows or not result.columns:
        return None
    picked = _pick_columns(result.columns, result.rows, value_column, label_column)
    if picked is None:
        return None
    label_idx, value_idx = picked

    ranked = sorted(result.rows, key=lambda r: float(r[value_idx]), reverse=True)
    total_value = sum(float(r[value_idx]) for r in ranked)
    n = len(ranked)
    high_cut = max(1, round(n * _HIGH_TIER_FRACTION))
    medium_cut = max(high_cut, round(n * _MEDIUM_TIER_FRACTION))

    items = []
    for position, row in enumerate(ranked[:max_items]):
        tier = "HIGH" if position < high_cut else "MEDIUM" if position < medium_cut else "LOW"
        items.append(AttentionItem(
            label=_cell(row[label_idx]),
            value=float(row[value_idx]),
            tier=tier,
            row={col: _cell(val) for col, val in zip(result.columns, row, strict=False)},
        ))

    return AttentionQueue(
        view_name=view_name,
        value_column=result.columns[value_idx],
        label_column=result.columns[label_idx],
        total_value=total_value,
        items=items,
        truncated=n > max_items,
    )
