"""Prompt-version contract tests.

These assert BEHAVIOUR at a version floor rather than an exact version, so a
routine prompt bump doesn't fail the suite. Pinning `== 4` here meant every
version bump broke three tests and taught people to ignore the failures.
The floor still catches the real risk: an active version that silently LOST a
rule the pipeline depends on.
"""

from app.prompts.loader import PromptManager


def test_active_intent_resolves_follow_ups_and_v1_still_renderable():
    prompts = PromptManager()
    assert prompts.active_version("orchestrator_intent") >= 4

    vars_ = {
        "data_as_of": "2026-06-27", "history": [], "message": "sort them by margin",
        "candidates": [], "playbooks": [],
    }
    active = prompts.render("orchestrator_intent", **vars_)
    assert "Follow-up resolution" in active.system
    assert "COMBINED" in active.system

    # Older versions stay renderable — the loader's version pinning is itself
    # the thing under test, so a rollback has to keep working.
    v1 = prompts.render("orchestrator_intent", version=1, **vars_)
    assert "Follow-up resolution" not in v1.system


def test_active_answer_keeps_no_speculation_and_aed_rules():
    prompts = PromptManager()
    assert prompts.active_version("orchestrator_answer") >= 3
    rendered = prompts.render(
        "orchestrator_answer",
        data_as_of="x", caution=None, message="q",
        source="s", truncated=False, row_count=0, data="(empty result set)",
        stats=None, glossary=None,
    )
    assert "Never infer business conclusions" in rendered.system
    assert "AED" in rendered.system


def test_greeting_template_and_greeting_route_rule():
    prompts = PromptManager()
    greeting = prompts.render("greeting_message")
    assert "Eliara" in greeting.user and "copilot" in greeting.user

    intent = prompts.render(
        "orchestrator_intent",
        data_as_of="x", history=[], message="hi", candidates=[], playbooks=[],
    )
    assert '"greeting"' in intent.system
