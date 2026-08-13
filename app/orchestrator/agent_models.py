"""Structured output contract for the bounded reasoning loop.

One `AgentStep` per iteration. The model either calls a tool or finishes.
Unlike `RoutingDecision` — which is a single terminal verdict — this is
re-issued each turn with the accumulated observations in front of it.
"""

from typing import Literal

from pydantic import BaseModel, Field

# Every tool the loop is allowed to invoke. The model cannot reach anything
# outside this set; unknown actions are rejected before execution.
AgentAction = Literal[
    "search_catalogue",
    "resolve_entity",
    "run_view",
    "run_sql",
    "glossary",
    "use_previous_result",
    "answer",
]


class AgentStep(BaseModel):
    """One iteration of the loop."""

    # Why this step. Kept in the trace; never shown to the user verbatim.
    thought: str = ""
    action: AgentAction

    # search_catalogue / glossary
    query: str | None = None

    # resolve_entity
    column: str | None = None
    value: str | None = None

    # run_view
    view_name: str | None = None
    filters: dict[str, str] = Field(default_factory=dict)

    # run_sql — a self-contained technical description, never the raw message
    task_description: str | None = None
    tables: list[str] = Field(default_factory=list)

    # use_previous_result — refine a result already gathered this turn or
    # carried in from the session's working set (index space: this turn's
    # observations come first, then the session's working set)
    refine_target: int | None = None
    refine: dict | None = None

    # answer
    text: str | None = None


class ToolCall(BaseModel):
    """What the loop actually did, for the trace."""

    step: int
    action: str
    argument: str = ""
    status: Literal["ok", "empty", "rejected", "error"] = "ok"
    row_count: int = 0
    detail: str | None = None
    # Provenance for governed sources.
    view_name: str | None = None
    assumption_status: str | None = None
    generated_sql: str | None = None
    elapsed_ms: int = 0


class AgentTrace(BaseModel):
    """The trace IS the provenance.

    A single-shot answer could name one approved view in `endpoint_used`. A
    composed answer cannot, so the ordered list of governed calls that produced
    it takes that role.
    """

    calls: list[ToolCall] = Field(default_factory=list)
    steps_used: int = 0
    stopped_because: Literal[
        "answered", "step_budget", "time_budget", "token_budget", "error"
    ] = "answered"
    input_tokens: int = 0
    output_tokens: int = 0

    @property
    def views_used(self) -> list[str]:
        seen: list[str] = []
        for call in self.calls:
            if call.view_name and call.view_name not in seen:
                seen.append(call.view_name)
        return seen

    @property
    def unvalidated(self) -> list[str]:
        """Governed sources whose formula is not signed off, plus custom SQL."""
        flagged = [
            call.view_name
            for call in self.calls
            if call.view_name
            and call.assumption_status
            and call.assumption_status != "APPROVED_LOGIC"
        ]
        if any(call.action == "run_sql" and call.status == "ok" for call in self.calls):
            flagged.append("custom query")
        return list(dict.fromkeys(flagged))
