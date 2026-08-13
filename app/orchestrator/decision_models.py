"""Structured output contract for Sonnet call #1 (routing)."""

from typing import Literal

from pydantic import BaseModel, Field

from app.execution.refine import RefineSpec


class SQLRequestSpec(BaseModel):
    task_description: str
    tables: list[str] = []


class RoutingDecision(BaseModel):
    decision: Literal[
        "use_view", "run_playbook", "needs_sql", "clarify", "out_of_scope",
        "greeting", "investigate", "refine",
    ]
    view_name: str | None = None
    # run_playbook: a named multi-step workflow, plus the entity it scopes to
    playbook: str | None = None
    playbook_entity: str | None = None
    endpoint_filters: dict[str, str] = Field(default_factory=dict)
    sql_request: SQLRequestSpec | None = None
    clarification: str | None = None
    # refine: operate on a result already in the session's working set —
    # zero DB hits, zero SQL generation. refine_target is the index into the
    # working set (0 = most recent). See app/execution/refine.py.
    refine_target: int = 0
    refine: RefineSpec | None = None

