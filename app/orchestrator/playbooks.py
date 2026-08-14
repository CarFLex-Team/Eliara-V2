"""Playbooks — reusable, multi-step analyses.

A single view answers a single question. Real management work needs several
views read together: "investigate this customer" means revenue AND margin AND
receivables AND churn risk, synthesised into one recommendation. Before this,
that took the user five separate questions and left them to join the answers
in their head.

A playbook is a declarative YAML file naming an ordered set of curated views
and how to combine them. Properties that matter:

  - **Deterministic step selection.** The views are fixed in the file, chosen
    once by a human from the governed catalogue. The model decides only
    *which playbook* to run, not which queries to issue.
  - **One LLM call, not N.** Every step executes in Python, results are
    aggregated in Python, and a single synthesis call writes the briefing.
    Cost is roughly one normal answer, not one per step.
  - **Graceful degradation.** A step whose view is missing from this database
    is skipped and reported, not fatal. Five of the catalogue's 100 questions
    are blocked on unloaded SAP sources; playbooks referencing them must still
    run with the steps that do work.
  - **Entity-scoped steps.** Steps marked ``filter_by`` receive the resolved
    entity, so "investigate MERSIN TRADE" filters every step to that customer
    while unfiltered steps supply portfolio context.
"""

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field

from app.core.logging import get_logger
from app.core.models import QueryResult
from app.execution.aggregate import summarise
from app.execution.formatter import to_llm_payload

log = get_logger("playbooks")

_PLAYBOOK_DIR = Path(__file__).parent / "definitions"

# Per-step payload budget. A playbook holds several result sets, so each step
# gets a slice of the answer prompt rather than the whole thing.
_STEP_PAYLOAD_CHARS = 1200
_STEP_ROW_CAP = 25


class PlaybookStep(BaseModel):
    step: str                       # human label, shown in the synthesis prompt
    view: str
    filter_by: str | None = None    # column receiving the resolved entity
    optional: bool = True           # False = playbook fails without it
    note: str | None = None         # why this step is here, for the model


class Playbook(BaseModel):
    name: str
    title: str
    description: str
    triggers: list[str] = Field(default_factory=list)
    requires_entity: bool = False
    entity_kind: str | None = None  # "customer" | "supplier" | "item"
    steps: list[PlaybookStep]
    synthesis: str                  # extra instructions for the answer model


class StepResult(BaseModel):
    step: str
    view: str
    status: str                     # ok | empty | missing | error
    row_count: int = 0
    note: str | None = None


class PlaybookRun(BaseModel):
    playbook: str
    title: str
    entity: str | None = None
    steps: list[StepResult] = Field(default_factory=list)
    payload: str = ""
    primary_result: Any = None      # QueryResult for the chart, if any

    @property
    def any_data(self) -> bool:
        return any(s.status == "ok" for s in self.steps)


class PlaybookLibrary:
    """Loads playbook definitions and reports which are runnable here."""

    def __init__(self, playbooks: dict[str, Playbook]) -> None:
        self._playbooks = playbooks

    @classmethod
    def load(cls, directory: Path | None = None) -> "PlaybookLibrary":
        directory = directory or _PLAYBOOK_DIR
        playbooks: dict[str, Playbook] = {}
        if directory.exists():
            for path in sorted(directory.glob("*.yaml")):
                try:
                    playbook = Playbook(**yaml.safe_load(path.read_text()))
                    playbooks[playbook.name] = playbook
                except Exception as exc:  # noqa: BLE001 - one malformed playbook YAML
                    # must not prevent every other playbook from loading.
                    log.warning(
                        "playbook_invalid", file=path.name, error=str(exc)
                    )
        log.info("playbooks_loaded", names=sorted(playbooks))
        return cls(playbooks)

    def __len__(self) -> int:
        return len(self._playbooks)

    def get(self, name: str) -> Playbook | None:
        return self._playbooks.get(name)

    def available(self, known_views: set[str]) -> list[Playbook]:
        """Playbooks with at least one runnable step in THIS database."""
        return [
            playbook
            for playbook in self._playbooks.values()
            if any(step.view in known_views for step in playbook.steps)
        ]

    def catalogue(self, known_views: set[str]) -> list[dict[str, Any]]:
        result = []
        for playbook in self._playbooks.values():
            runnable = [s for s in playbook.steps if s.view in known_views]
            result.append(
                {
                    "name": playbook.name,
                    "title": playbook.title,
                    "description": playbook.description,
                    "requires_entity": playbook.requires_entity,
                    "entity_kind": playbook.entity_kind,
                    "steps_total": len(playbook.steps),
                    "steps_runnable": len(runnable),
                    "runnable": bool(runnable),
                }
            )
        return sorted(result, key=lambda p: p["name"])


def run_playbook(
    playbook: Playbook,
    executor,
    known_views: set[str],
    entity: str | None = None,
) -> PlaybookRun:
    """Execute every runnable step and build one combined payload.

    No LLM is involved here. The caller makes a single synthesis call with the
    payload this returns.
    """
    run = PlaybookRun(playbook=playbook.name, title=playbook.title, entity=entity)
    sections: list[str] = []

    for step in playbook.steps:
        if step.view not in known_views:
            run.steps.append(
                StepResult(
                    step=step.step, view=step.view, status="missing",
                    note="not present in this database",
                )
            )
            continue

        filters = None
        if step.filter_by and entity:
            filters = {step.filter_by: entity}

        try:
            result: QueryResult = executor.run_view(
                step.view, filters, limit=_STEP_ROW_CAP
            )
        except Exception as exc:  # noqa: BLE001 - one broken step must not
            # abort the whole multi-step playbook run.
            log.warning(
                "playbook_step_failed", playbook=playbook.name,
                view=step.view, error=type(exc).__name__,
            )
            run.steps.append(
                StepResult(step=step.step, view=step.view, status="error")
            )
            continue

        if not result.rows:
            run.steps.append(
                StepResult(step=step.step, view=step.view, status="empty")
            )
            sections.append(f"### {step.step}\n(no matching rows)")
            continue

        run.steps.append(
            StepResult(
                step=step.step, view=step.view, status="ok",
                row_count=result.row_count, note=step.note,
            )
        )
        if run.primary_result is None:
            run.primary_result = result

        payload, shown = to_llm_payload(result, max_chars=_STEP_PAYLOAD_CHARS)
        block = [f"### {step.step}"]
        if step.note:
            block.append(f"_{step.note}_")
        block.append(payload)
        stats = summarise(result, shown)
        if stats:
            block.append(f"Totals over all {result.row_count} rows:\n{stats}")
        sections.append("\n".join(block))

    run.payload = "\n\n".join(sections)
    log.info(
        "playbook_complete",
        playbook=playbook.name,
        entity=entity,
        ok=sum(1 for s in run.steps if s.status == "ok"),
        skipped=sum(1 for s in run.steps if s.status != "ok"),
    )
    return run
