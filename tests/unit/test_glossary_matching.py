"""Regression tests for Orchestrator._glossary_for().

Covers https://github.com/Mosapmohamd/Eliara-V2/issues/2 — a churn answer
was generated from the model's own assumed definition rather than the
company's glossary. The glossary-injection mechanism itself already
existed (_glossary_for), but its matching was a one-directional substring
check: it only matched when the glossary TERM appeared verbatim inside the
question. A multi-word term like "customer churn" never matched a question
that just said "churn", because "customer churn" is not a substring of
"churn". These tests pin the fixed two-tier match (exact substring, plus a
distinctive-word fallback) against that exact failure shape.
"""

from types import SimpleNamespace

from app.core.config import Settings
from app.orchestrator.conversation import InMemoryConversationStore
from app.orchestrator.orchestrator import Orchestrator
from app.prompts.loader import PromptManager


class _StubLLM:
    async def structured_call(self, prompt, output_model, *, model, max_tokens=1000):
        raise AssertionError("not exercised in these tests")

    async def call(self, prompt, *, model, max_tokens=1500, temperature=0.2, **kwargs):
        raise AssertionError("not exercised in these tests")


def _orchestrator_with_glossary(glossary: dict[str, str]) -> Orchestrator:
    """An Orchestrator whose _glossary_for() reads from a fixed glossary,
    without needing a real executor/discovery pipeline — _glossary_for only
    touches self._index.glossary."""
    orch = Orchestrator(
        retriever=None,
        index=SimpleNamespace(glossary=glossary),
        executor=None,
        prompts=PromptManager(),
        conversations=InMemoryConversationStore(),
        llm=_StubLLM(),
        settings=Settings(_env_file=None),
    )
    return orch


def test_exact_term_in_question_still_matches():
    """Baseline: the original behavior (term appears verbatim in the
    question) must keep working."""
    orch = _orchestrator_with_glossary(
        {"dead stock": "Items with no sales for 12+ months"}
    )
    result = orch._glossary_for("what counts as dead stock?")
    assert result is not None
    assert "dead stock" in result
    assert "12+ months" in result


def test_multiword_term_matches_a_shorter_question_mentioning_it():
    """The reported failure shape: glossary term is multi-word
    ("customer churn"), but the actual question is shorter and only
    contains part of it ("churn"). Must now match."""
    orch = _orchestrator_with_glossary(
        {"customer churn": "Customers with no orders in the trailing 90 days"}
    )
    result = orch._glossary_for("what's our churn this quarter?")
    assert result is not None
    assert "customer churn" in result
    assert "90 days" in result


def test_multiword_term_matches_regardless_of_word_order_in_question():
    orch = _orchestrator_with_glossary(
        {"net revenue": "Invoice revenue minus credit notes"}
    )
    result = orch._glossary_for("show me revenue by month")
    assert result is not None
    assert "net revenue" in result


def test_unrelated_question_gets_no_glossary_block():
    orch = _orchestrator_with_glossary(
        {"dead stock": "Items with no sales for 12+ months"}
    )
    assert orch._glossary_for("who are our top customers?") is None


def test_empty_glossary_returns_none():
    orch = _orchestrator_with_glossary({})
    assert orch._glossary_for("what is dead stock?") is None


def test_short_generic_words_in_a_term_do_not_cause_false_matches():
    """A term containing only short/generic words (e.g. under 4 chars)
    should not create noisy false-positive matches purely on those words —
    the distinctive-word fallback only considers words of length >= 4."""
    orch = _orchestrator_with_glossary({"net qty": "Net quantity after returns"})
    # "net" and "qty" are both short; a question that shares neither the
    # exact phrase nor any distinctive (>=4 char) word should not match.
    result = orch._glossary_for("how many customers do we have?")
    assert result is None


def test_multiple_matching_terms_are_all_included_up_to_five():
    orch = _orchestrator_with_glossary(
        {
            "dead stock": "def 1",
            "customer churn": "def 2",
            "net revenue": "def 3",
        }
    )
    result = orch._glossary_for("compare churn and revenue trends, and check dead stock too")
    assert result is not None
    assert result.count("\n") == 2  # 3 lines, 2 newlines between them
    assert "dead stock" in result
    assert "customer churn" in result
    assert "net revenue" in result
