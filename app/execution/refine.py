"""Deterministic refinement of an already-fetched result.

The gap this closes: "sort those by margin" had no cheaper path than
`needs_sql` — regenerate SQL, re-validate it, re-run it, to sort 20 rows the
system already had in memory a message ago.

A refinement never touches the database and never calls an LLM for the data
operation itself. It is pure Python over the stored rows. That makes it the
fastest path in the system by a wide margin, not just a convenience.

Deliberately narrow: sort, equality/threshold filter, limit. A request that
needs a column not in the stored result (join, new aggregate, a metric that
was never selected) is NOT expressible here — `RefineSpec.column` /
`RefineSpec.filters` keys are validated against the stored columns, and an
unknown column is a hard rejection, not a silent no-op. The routing prompt is
responsible for falling back to `needs_sql` when a refinement can't cover it.
"""

from typing import Literal

from pydantic import BaseModel, Field

from app.core.models import QueryResult

_OPS = {"eq", "gt", "gte", "lt", "lte", "ne"}


class RefineFilter(BaseModel):
    column: str
    op: Literal["eq", "gt", "gte", "lt", "lte", "ne"] = "eq"
    value: str


class RefineSpec(BaseModel):
    sort_by: str | None = None
    sort_desc: bool = False
    filters: list[RefineFilter] = Field(default_factory=list)
    limit: int | None = None


class RefineError(Exception):
    """A refinement referenced a column the stored result doesn't have."""

    def __init__(self, column: str, available: list[str]):
        self.column = column
        self.available = available
        super().__init__(f"{column!r} is not in this result (have: {available})")


def _coerce(value, like) -> object:
    """Compare the filter value in the stored column's own type."""
    if isinstance(like, bool) or like is None:
        return value
    if isinstance(like, (int, float)):
        try:
            return float(value)
        except (TypeError, ValueError):
            return value
    return str(value)


def _passes(cell, op: str, target) -> bool:
    try:
        if op == "eq":
            return cell == target
        if op == "ne":
            return cell != target
        if cell is None:
            return False
        if op == "gt":
            return cell > target
        if op == "gte":
            return cell >= target
        if op == "lt":
            return cell < target
        if op == "lte":
            return cell <= target
    except TypeError:
        return False
    return False


def apply_refinement(result: QueryResult, spec: RefineSpec) -> QueryResult:
    """Sort / filter / limit rows already held in memory. Validates columns
    before touching a single row, so a bad request fails clearly rather than
    silently returning the unfiltered set."""
    columns = result.columns
    index = {name: i for i, name in enumerate(columns)}

    if spec.sort_by is not None and spec.sort_by not in index:
        raise RefineError(spec.sort_by, columns)
    for f in spec.filters:
        if f.column not in index:
            raise RefineError(f.column, columns)

    rows = list(result.rows)

    for f in spec.filters:
        pos = index[f.column]
        sample = next((r[pos] for r in rows if r[pos] is not None), None)
        target = _coerce(f.value, sample)
        rows = [r for r in rows if _passes(r[pos], f.op, target)]

    if spec.sort_by is not None:
        pos = index[spec.sort_by]
        # Nulls sort last regardless of direction — reverse=True should flip
        # value order, not push nulls to the front.
        present = [r for r in rows if r[pos] is not None]
        missing = [r for r in rows if r[pos] is None]
        present.sort(key=lambda r: r[pos], reverse=spec.sort_desc)
        rows = present + missing

    total_after_filter = len(rows)
    truncated = result.truncated
    if spec.limit is not None and len(rows) > spec.limit:
        rows = rows[: spec.limit]
        truncated = True

    return QueryResult(
        columns=columns,
        rows=rows,
        row_count=total_after_filter,
        truncated=truncated,
        source=result.source,
        object_name=result.object_name,
        elapsed_ms=0,
    )
