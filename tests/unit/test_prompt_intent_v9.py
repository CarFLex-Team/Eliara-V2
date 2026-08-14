"""Pins the refine-vs-fresh-query scope guard added in intent_v9.

Covers https://github.com/Mosapmohamd/Eliara-V2/issues/3 — the refine rule
only ever checked whether the working set's COLUMNS covered a follow-up
question, never whether its ROW SCOPE did. A working-set entry from "top 10
customers by revenue" only holds the 10 rows its own query's LIMIT let
through; a follow-up asking for the full/company-wide picture was still
routed to refine because the columns matched, silently answering from a
too-narrow subset. This test does not (and cannot, without a live model)
prove routing behavior changes — it pins that the new rule text exists and
survives future prompt-version bumps, per the version-floor pattern used
elsewhere in this test suite (see test_prompt_intent_v2.py).
"""

from app.prompts.loader import PromptManager


def test_refine_rule_includes_the_row_scope_guard():
    prompts = PromptManager()
    assert prompts.active_version("orchestrator_intent") >= 9

    rendered = prompts.render(
        "orchestrator_intent",
        data_as_of="2026-06-27", history=[], message="sort them by margin",
        candidates=[], playbooks=[],
    )
    # The two places the guard was added: the standalone refine rule, and
    # the earlier follow-up-resolution bullet that first decides
    # refine-vs-fresh-query.
    assert "WIDER scope" in rendered.system
    assert "top-N ranking" in rendered.system
    assert "broader row scope" in rendered.system

    # v8 (pre-fix) must NOT have this guard — confirms the test is actually
    # discriminating on the new rule, not matching something already present.
    v8 = prompts.render(
        "orchestrator_intent", version=8,
        data_as_of="2026-06-27", history=[], message="sort them by margin",
        candidates=[], playbooks=[],
    )
    assert "WIDER scope" not in v8.system
