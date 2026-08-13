"""Retrieval evaluation harness — the M2 quality gate and permanent regression net.

Ground truth = the database's own chatbot_question_view_registry: for each
enabled canonical question, does the retriever put the mapped view in top-1 /
top-3?
"""

from pydantic import BaseModel

from app.discovery.models import RegistryEntry
from app.discovery.search import HybridRetriever


class EvalMiss(BaseModel):
    question_id: int
    query: str
    expected_view: str
    got_top3: list[str]


class EvalReport(BaseModel):
    total: int
    skipped_missing_view: int
    top1_hits: int
    top3_hits: int
    misses: list[EvalMiss]

    @property
    def top1_accuracy(self) -> float:
        return self.top1_hits / self.total if self.total else 0.0

    @property
    def top3_accuracy(self) -> float:
        return self.top3_hits / self.total if self.total else 0.0


def evaluate(
    retriever: HybridRetriever,
    items: list[tuple[int, str, str]],  # (question_id, query, expected_view)
    known_objects: set[str],
) -> EvalReport:
    total = top1 = top3 = skipped = 0
    misses: list[EvalMiss] = []
    for question_id, query, expected in items:
        if expected not in known_objects:
            skipped += 1
            continue
        total += 1
        got = [c.view_name for c in retriever.search(query, k=3)]
        if got and got[0] == expected:
            top1 += 1
        if expected in got:
            top3 += 1
        else:
            misses.append(
                EvalMiss(question_id=question_id, query=query, expected_view=expected, got_top3=got)
            )
    return EvalReport(
        total=total, skipped_missing_view=skipped, top1_hits=top1, top3_hits=top3, misses=misses
    )


def canonical_items(registry: list[RegistryEntry]) -> list[tuple[int, str, str]]:
    return [
        (e.question_id, e.canonical_question, e.view_name) for e in registry if e.enabled
    ]
