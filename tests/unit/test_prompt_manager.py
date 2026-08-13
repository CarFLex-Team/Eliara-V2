import pytest
from jinja2 import UndefinedError

from app.prompts.loader import PromptError, PromptManager


@pytest.fixture(scope="module")
def prompts():
    return PromptManager()


def test_all_shipped_templates_render(prompts):
    """Contract test: every production template renders with its variables."""
    intent = prompts.render(
        "orchestrator_intent",
        data_as_of="2026-06-27",
        history=[{"role": "user", "content": "hi"}],
        message="top customers?",
        candidates=[{
            "view_name": "vw_q002_x", "category": "question_view",
            "canonical_question": "Who are the top 10 customers?",
            "requires_endpoint_filter": False, "columns": ["customer_code"],
        }],
        playbooks=[],
    )
    # Version floor, not an exact pin — a routine prompt bump must not fail
    # this contract test.
    assert intent.name == "orchestrator_intent"
    assert intent.version >= 4
    assert "vw_q002_x" in intent.user
    assert "use_view" in intent.system

    answer = prompts.render(
        "orchestrator_answer",
        data_as_of="2026-06-27", caution=None, message="q",
        source="curated view", truncated=False, row_count=3, data="| a | b |",
        stats=None, glossary=None,
    )
    assert "2026-06-27" in answer.system

    sql = prompts.render(
        "sqlgen_generate",
        max_rows=500, task_description="avg invoice per warehouse",
        tables=[{"name": "fact_ai_sales_net", "kind": "table", "columns": ["a", "b"]}],
    )
    assert "LIMIT 500" in sql.system


def test_caution_toggles_wording(prompts):
    with_caution = prompts.render(
        "orchestrator_answer",
        data_as_of="x", caution="REVIEW_FORMULA", message="q",
        source="s", truncated=False, row_count=0, data="d",
        stats=None, glossary=None,
    )
    assert "indicative" in with_caution.system


def test_missing_variable_raises(prompts):
    with pytest.raises(UndefinedError):
        prompts.render("orchestrator_answer", data_as_of="x")


def test_unknown_prompt_raises(prompts):
    with pytest.raises(PromptError):
        prompts.render("nope")
