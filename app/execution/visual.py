"""QueryResult -> chart shape, shared by the legacy /ask endpoint and the
streaming endpoint's `visual` event.

Extracted from app/api/legacy.py rather than duplicated — the trend/ranking/
table detection logic (and its documented reasoning about why `distribution`
is NOT auto-detected) needs to stay in exactly one place, or the two callers
drift apart the first time either one gets a bug fix.
"""

from typing import Any

from app.core.models import QueryResult

# Display caps. The chat bubble is ~70vw; beyond this a table stops being
# readable and just inflates the payload.
_VISUAL_MAX_ROWS = 50
_VISUAL_MAX_COLS = 8
_RANKING_MAX_ROWS = 15

# A 2-column (period, numeric) result where the first column looks like a
# calendar unit is a line, not a leaderboard — "revenue by month" read as a
# bar-chart ranking is misleading regardless of row count. Checked BEFORE the
# ranking test so period-shaped data never falls through to it.
_PERIOD_HINTS = ("year", "month", "quarter", "week", "date", "period")

# A line through one point is not a trend. Two is the minimum that shows
# direction; see _detect_trend for the comparison-query case this guards.
_TREND_MIN_POINTS = 2


def _cell(value: Any) -> str:
    """Format a cell as a NON-EMPTY display string.

    Table renderers commonly do `row[i] || row[col] || ""`, so a numeric 0
    or a False would render as an empty cell. Returning strings keeps every
    value truthy and therefore visible.
    """
    if value is None:
        return "—"
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, float):
        return f"{value:,.2f}" if value % 1 else f"{value:,.0f}"
    if isinstance(value, int):
        return f"{value:,}"
    text = str(value).strip()
    return text or "—"


def _is_numeric(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _looks_like_period(column: str) -> bool:
    lowered = column.lower()
    return any(hint in lowered for hint in _PERIOD_HINTS)


# A 2-column-only trend rule missed the realistic case: a monthly view with
# auxiliary columns alongside the metric (period_start/period_end date-range
# columns next to year_month, a second metric like gross_profit next to
# revenue). This mirrors aggregate.py's _NON_METRIC_HINTS convention
# independently rather than importing it — chart-shape selection and
# text-summary stats solve the same "don't treat an id/code/pct as a value"
# problem for different reasons, and keeping them decoupled means a tuning
# change to one doesn't silently change the other.
_NON_METRIC_HINTS = ("_id", "_code", "rank", "score", "_pct", "percent", "_no", "number")


def _looks_like_metric(column: str) -> bool:
    lowered = column.lower()
    return not any(hint in lowered for hint in _NON_METRIC_HINTS)


def _pretty_title(view_name: str | None) -> str:
    if not view_name:
        return "Results"
    name = view_name
    for prefix in ("vw_q", "vw_ai_", "vw_gold_", "vw_official_", "vw_", "fact_", "dim_", "engine_"):
        if name.startswith(prefix):
            name = name[len(prefix):]
            break
    name = name.lstrip("0123456789_")
    return name.replace("_", " ").strip().title() or "Results"


def _detect_trend(columns: list[str], rows: list[tuple]) -> tuple[int, int] | None:
    """Find (period_column_index, metric_column_index), or None.

    The period column must be the FIRST column — a period buried mid-result
    is a coincidence, not the shape of a time-series query. The metric is the
    first later column that is numeric across every row and doesn't look like
    an id/code/percentage; other period-like columns (period_start, period_end
    alongside year_month — a real production view shape, not hypothetical)
    are skipped rather than mistaken for the value.

    At least two points required. A single row is never a trend, however
    period-like its first column looks. This matters because a comparison
    query ("Feb 2026 vs the historical average") returns ONE row whose first
    column is a synthetic label — often literally named `period` — with the
    values to compare sitting in SEPARATE COLUMNS of that same row. Charting
    that as a trend produced a line with exactly one dot on it and silently
    dropped the number being compared against, which is worse than useless:
    it looks like an answer. Such results fall through to `table`, which
    shows every column side by side.
    """
    if len(rows) < _TREND_MIN_POINTS or not columns or not _looks_like_period(columns[0]):
        return None
    for i, column in enumerate(columns):
        if i == 0 or _looks_like_period(column):
            continue
        if _looks_like_metric(column) and all(_is_numeric(r[i]) for r in rows):
            return 0, i
    return None


def build_visual(result: QueryResult | None, view_name: str | None) -> dict[str, Any] | None:
    """Turn a QueryResult into a chart/table block the UI can render.

    Three shapes, checked in this order:
      - trend:    first column is a calendar unit (year, month, quarter...),
                  plus at least one other column that's numeric across every
                  row and doesn't look like an id/code/percentage. Extra
                  columns — a second metric, a period_start/period_end date
                  range alongside year_month — don't block it; the first
                  qualifying metric column is charted and the rest are
                  ignored. Checked BEFORE ranking so "revenue by month" is a
                  line, not a bar-chart leaderboard, regardless of row count
                  or how many other columns the view carries.
      - ranking:  EXACTLY 2 columns, second numeric, few enough rows to chart
      - table:    everything else
    Anything the UI cannot render is better sent as plain prose in the answer.

    `label`/`value` keys match RankingView.tsx exactly (verified against the
    frontend source — see test_legacy_frontend_contract.py). `trend` reuses
    that same convention since MessageBubble.tsx already dispatches to a
    trend component, though no source for TrendView.tsx has been available
    to verify the exact prop shape against — confirm it renders correctly
    before relying on this in production.

    NOT attempted here: `distribution`. A 2-column (label, positive-number)
    result is structurally IDENTICAL whether it's "top 5 customers" (a
    ranking) or "sales by 5 regions" (a distribution) — there is no reliable
    shape-only signal to tell them apart. `result.truncated` looks like it
    could help but doesn't: it only reflects the EXECUTOR's hard row cap
    (500), not a view's own `TOP 10` in its SQL, so a small curated "top N"
    view is indistinguishable from a genuine breakdown by shape alone. Fixing
    this needs an actual signal — either the routing model tagging intent
    from phrasing ("breakdown", "share of", "distribution") or a hint in the
    view registry — not a heuristic. Shape-based guessing here would silently
    turn some legitimate small rankings into wrong-looking pie charts, which
    is worse than not having distribution charts at all.
    """
    if result is None or not result.rows or not result.columns:
        return None

    columns = list(result.columns)[:_VISUAL_MAX_COLS]
    rows = result.rows[:_VISUAL_MAX_ROWS]
    title = _pretty_title(view_name)

    trend = _detect_trend(result.columns, rows)
    if trend is not None:
        label_idx, value_idx = trend
        ordered = sorted(rows, key=lambda r: _cell(r[label_idx]))
        return {
            "type": "trend",
            "title": title,
            "trend": [
                {"label": _cell(r[label_idx])[:40], "value": round(float(r[value_idx]), 2)}
                for r in ordered
            ],
        }

    two_col_numeric = len(result.columns) == 2 and all(_is_numeric(r[1]) for r in rows)

    if two_col_numeric and len(rows) <= _RANKING_MAX_ROWS:
        return {
            "type": "ranking",
            "title": title,
            "ranking": [
                {"label": _cell(r[0])[:40], "value": round(float(r[1]), 2)} for r in rows
            ],
        }

    return {
        "type": "table",
        "title": title,
        "columns": columns,
        "rows": [[_cell(v) for v in row[:_VISUAL_MAX_COLS]] for row in rows],
    }
