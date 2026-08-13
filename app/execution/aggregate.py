"""Deterministic statistics over the FULL result set, computed before narration.

Observed in production, answering "talk me about our deadstock":

    "The top 13 items alone lock up approximately AED 188,683 ...
     the total capital locked is likely substantially higher than
     what is visible here."

The view returned 500 rows; ``payload_max_chars`` trimmed the prompt to 13; the
answer model narrated the 13 it could see and hedged about the rest. It could
not add up its own result set.

Nothing here needs an LLM. Python computes totals, shares, concentration and
group breakdowns over every row, and the result is handed to the answer prompt
alongside the (still truncated) sample rows. The answer then says "AED 4.2M
across 500 SKUs, 61% concentrated in lighting" instead of describing a sample.

Design rules:
  - Facts only. No interpretation, no thresholds, no editorialising — that is
    the answer model's job, and it must not be pre-empted by a heuristic.
  - Only over the rows actually returned. If the query itself was capped by
    ``max_rows``, that is stated explicitly so the model cannot overclaim.
"""

from collections import defaultdict

from app.core.models import QueryResult

# A column with more distinct values than this is an identifier, not a category.
_MAX_GROUP_CARDINALITY = 25
_TOP_GROUPS = 5
_CONCENTRATION_N = 10

# Columns that are numeric but never worth summing.
_NON_METRIC_HINTS = (
    "_id", "_code", "year", "month", "day", "rank", "score", "_pct", "percent",
    "margin_pct", "days", "_no", "number",
)


def _is_number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _fmt(value: float) -> str:
    if abs(value) >= 1000:
        return f"{value:,.0f}"
    if float(value).is_integer():
        return f"{value:,.0f}"
    return f"{value:,.2f}"


def _looks_like_metric(column: str) -> bool:
    lowered = column.lower()
    return not any(hint in lowered for hint in _NON_METRIC_HINTS)


def summarise(result: QueryResult, rows_shown: int) -> str | None:
    """Return a markdown stats block, or None when there is nothing useful.

    ``rows_shown`` is how many rows made it into the prompt sample, so the block
    can tell the model exactly how much it is NOT seeing.
    """
    if not result.rows or not result.columns:
        return None
    # With a handful of rows the model can see everything; stats add noise.
    if result.row_count <= 3:
        return None

    numeric_cols: list[tuple[int, str]] = []
    for i, column in enumerate(result.columns):
        values = [r[i] for r in result.rows if _is_number(r[i])]
        if len(values) >= max(2, result.row_count // 2) and _looks_like_metric(column):
            numeric_cols.append((i, column))

    lines: list[str] = []
    hidden = result.row_count - rows_shown
    if hidden > 0:
        lines.append(
            f"- Rows: {result.row_count:,} total "
            f"({rows_shown:,} shown above, {hidden:,} not shown — "
            f"the totals below cover ALL {result.row_count:,} rows)"
        )
    else:
        lines.append(f"- Rows: {result.row_count:,} (all shown above)")

    if result.truncated:
        lines.append(
            f"- NOTE: the query itself hit its {result.row_count:,}-row cap; "
            "more rows exist in the database beyond these."
        )

    primary: tuple[int, str] | None = None
    for i, column in numeric_cols:
        values = [r[i] for r in result.rows if _is_number(r[i])]
        total = sum(values)
        lines.append(
            f"- {column}: total {_fmt(total)}, "
            f"mean {_fmt(total / len(values))}, "
            f"min {_fmt(min(values))}, max {_fmt(max(values))}"
        )
        if primary is None and total > 0:
            primary = (i, column)

    # Concentration on the leading metric — the "how lopsided is this" number.
    if primary is not None and result.row_count > _CONCENTRATION_N:
        idx, column = primary
        values = sorted(
            (r[idx] for r in result.rows if _is_number(r[idx])), reverse=True
        )
        total = sum(values)
        if total > 0:
            top = sum(values[:_CONCENTRATION_N])
            lines.append(
                f"- Concentration: top {_CONCENTRATION_N} rows hold "
                f"{_fmt(top)} of {column} ({top / total * 100:.1f}% of the total)"
            )

    # Breakdown by any low-cardinality categorical column.
    if primary is not None:
        idx, metric = primary
        for i, column in enumerate(result.columns):
            if i == idx or _is_number(result.rows[0][i]):
                continue
            buckets: dict[str, float] = defaultdict(float)
            counts: dict[str, int] = defaultdict(int)
            for row in result.rows:
                key = str(row[i]).strip() if row[i] is not None else "(none)"
                buckets[key] += row[idx] if _is_number(row[idx]) else 0
                counts[key] += 1
            if not 1 < len(buckets) <= _MAX_GROUP_CARDINALITY:
                continue
            total = sum(buckets.values())
            if total <= 0:
                continue
            ranked = sorted(buckets.items(), key=lambda kv: kv[1], reverse=True)
            parts = [
                f"{k} {_fmt(v)} ({v / total * 100:.0f}%, {counts[k]} rows)"
                for k, v in ranked[:_TOP_GROUPS]
            ]
            suffix = "" if len(ranked) <= _TOP_GROUPS else f", +{len(ranked) - _TOP_GROUPS} more"
            lines.append(f"- {metric} by {column}: " + "; ".join(parts) + suffix)
            break  # one breakdown is context; several are clutter

    return "\n".join(lines) if len(lines) > 1 else None
