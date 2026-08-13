"""Shared domain models passed between layers."""

from typing import Literal

from pydantic import BaseModel


class DateRange(BaseModel):
    first_date: str
    last_date: str


class QueryResult(BaseModel):
    columns: list[str]
    rows: list[tuple]
    row_count: int
    truncated: bool
    source: Literal["view", "generated_sql", "metadata"]
    object_name: str
    elapsed_ms: int


class ViewCandidate(BaseModel):
    view_name: str
    kind: Literal["table", "view"]
    category: str
    canonical_question: str | None = None
    columns: list[str] = []
    formula_version: str | None = None
    assumption_status: str | None = None
    requires_endpoint_filter: bool = False
    time_scope_rule: str | None = None
    score: float = 0.0
