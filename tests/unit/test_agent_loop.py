"""Bounded reasoning loop tests.

The properties that matter, in order of how badly a regression would hurt:

  1. The loop ALWAYS lands. Whichever budget binds, the user gets an answer.
  2. The safety surface is unchanged. The model gains iterations, not reach:
     off-catalogue views are rejected, invalid SQL is rejected, filters are
     cross-checked against the view's real columns.
  3. A rejection is information. The reason goes back as an observation and
     the loop recovers instead of failing the turn.
  4. The trace carries provenance, since a composed answer cannot name one
     approved view.
  5. Parity: on a question a playbook already covers, the loop reaches the
     same governed views.
"""

import sqlite3

import pytest

from app.core.config import Settings
from app.discovery.embedder import HashingEmbedder
from app.discovery.index import MetadataIndex
from app.discovery.metadata_loader import MetadataLoader
from app.discovery.search import HybridRetriever
from app.execution.executor import ReadOnlyExecutor
from app.llm.anthropic_client import LLMResponse
from app.orchestrator.agent import ReasoningAgent
from app.orchestrator.agent_models import AgentStep
from app.orchestrator.playbooks import PlaybookLibrary
from app.prompts.loader import PromptManager


class ScriptedLLM:
    """structured_call pops the next scripted AgentStep; call returns text."""

    def __init__(self, steps: list[AgentStep], landing: str = "Partial briefing."):
        self.steps = list(steps)
        self.landing = landing
        self.step_prompts = []
        self.landing_prompts = []

    async def structured_call(self, prompt, output_model, *, model, max_tokens=1000):
        self.step_prompts.append(prompt)
        step = self.steps.pop(0)
        return step, LLMResponse(
            text="{}", model=model, input_tokens=500, output_tokens=40,
            latency_ms=5, prompt_tag=prompt.tag,
        )

    async def call(self, prompt, *, model, max_tokens=1500, temperature=0.2, **kwargs):
        self.landing_prompts.append(prompt)
        return LLMResponse(
            text=self.landing, model=model, input_tokens=400, output_tokens=60,
            latency_ms=5, prompt_tag=prompt.tag,
        )


class ScriptedSQLGen:
    """Stands in for SQLGenerator — same async .generate(task, slice) contract."""

    def __init__(self, sql: str):
        self.sql = sql

    async def generate(self, task_description, schema_slice, previous_error=None):
        return self.sql, LLMResponse(
            text=self.sql, model="haiku", input_tokens=300, output_tokens=25,
            latency_ms=4, prompt_tag="sqlgen_generate@v2",
        )


def _agent(executor, llm, settings=None, sqlgen=None, entities=None) -> ReasoningAgent:
    objects, registry, glossary, fingerprint = MetadataLoader(executor).load()
    index = MetadataIndex(objects, registry, glossary, fingerprint)
    return ReasoningAgent(
        retriever=HybridRetriever(index, HashingEmbedder()),
        index=index,
        executor=executor,
        prompts=PromptManager(),
        llm=llm,
        settings=settings or Settings(_env_file=None),
        entities=entities,
        sqlgen=sqlgen,
    )


# --------------------------------------------------------------- the loop lands


async def test_loop_returns_the_models_answer_and_stops(executor):
    llm = ScriptedLLM([
        AgentStep(action="run_view", view_name="vw_q002_top_10_customers_by_lifetime_revenue"),
        AgentStep(action="answer", text="Alpha Trading leads at AED 7,600."),
    ])
    answer, trace = await _agent(executor, llm).run("who are our top customers?")

    assert answer == "Alpha Trading leads at AED 7,600."
    assert trace.stopped_because == "answered"
    assert trace.steps_used == 2
    assert len(trace.calls) == 1


async def test_step_budget_forces_a_landing_rather_than_returning_nothing(executor):
    settings = Settings(_env_file=None, agent_max_steps=2)
    llm = ScriptedLLM(
        [AgentStep(action="run_view",
                   view_name="vw_q002_top_10_customers_by_lifetime_revenue")] * 2,
        landing="Partial: top customers pulled, margin not yet checked.",
    )
    answer, trace = await _agent(executor, llm, settings).run("full customer picture")

    assert trace.stopped_because == "step_budget"
    assert answer.startswith("Partial:")
    assert llm.landing_prompts, "a forced landing must still call the synthesis prompt"
    assert llm.landing_prompts[0].name == "agent_synthesis"


async def test_time_budget_binds_before_any_tool_runs(executor):
    settings = Settings(_env_file=None, agent_time_budget_s=-1)
    llm = ScriptedLLM([AgentStep(action="answer", text="unreachable")])
    answer, trace = await _agent(executor, llm, settings).run("anything")

    assert trace.stopped_because == "time_budget"
    assert trace.calls == []
    assert "could not gather enough data" in answer


async def test_landing_prompt_is_told_the_answer_is_partial(executor):
    settings = Settings(_env_file=None, agent_max_steps=1)
    llm = ScriptedLLM([AgentStep(action="search_catalogue", query="customers")])
    await _agent(executor, llm, settings).run("how are we doing?")

    rendered = llm.landing_prompts[0].system
    assert "cut short" in rendered
    assert "remains unchecked" in rendered


# ------------------------------------------------------- the surface is governed


async def test_view_outside_the_catalogue_is_rejected(executor):
    llm = ScriptedLLM([
        AgentStep(action="run_view", view_name="sap_oitm_raw"),
        AgentStep(action="answer", text="Recovered without it."),
    ])
    answer, trace = await _agent(executor, llm).run("show me raw sap data")

    assert trace.calls[0].status == "rejected"
    assert "not in the catalogue" in trace.calls[0].detail
    assert answer == "Recovered without it."


async def test_a_rejection_is_fed_back_as_an_observation(executor):
    llm = ScriptedLLM([
        AgentStep(action="run_view", view_name="batch_09_import_evidence"),
        AgentStep(action="answer", text="Used a different route."),
    ])
    await _agent(executor, llm).run("evidence please")

    second = llm.step_prompts[1].user
    assert "REJECTED" in second
    assert "not in the catalogue" in second


async def test_filters_are_cross_checked_against_the_views_real_columns(executor):
    llm = ScriptedLLM([
        AgentStep(
            action="run_view",
            view_name="vw_q002_top_10_customers_by_lifetime_revenue",
            filters={"customer_name": "Alpha Trading", "not_a_column": "x"},
        ),
        AgentStep(action="answer", text="done"),
    ])
    _, trace = await _agent(executor, llm).run("alpha trading revenue")

    assert trace.calls[0].status == "ok"
    assert trace.calls[0].row_count == 1


async def test_generated_sql_still_passes_through_the_ast_gate(executor):
    sqlgen = ScriptedSQLGen("DROP TABLE fact_ai_sales_net")
    llm = ScriptedLLM([
        AgentStep(action="run_sql", task_description="remove the sales table"),
        AgentStep(action="answer", text="Refused, answered from views instead."),
    ])
    _, trace = await _agent(executor, llm, sqlgen=sqlgen).run("delete everything")

    assert trace.calls[0].status == "rejected"
    assert trace.calls[0].generated_sql is None


async def test_accepted_sql_is_recorded_from_the_validated_ast(executor):
    sqlgen = ScriptedSQLGen("SELECT customer_name, net_revenue FROM fact_ai_sales_net")
    llm = ScriptedLLM([
        AgentStep(action="run_sql", task_description="revenue per customer",
                  tables=["fact_ai_sales_net"]),
        AgentStep(action="answer", text="done"),
    ])
    _, trace = await _agent(executor, llm, sqlgen=sqlgen).run("revenue by customer")

    call = trace.calls[0]
    assert call.status == "ok"
    assert call.generated_sql.startswith("SELECT")
    assert "LIMIT" in call.generated_sql


# ------------------------------------------------------------------ observations


async def test_empty_result_is_reported_as_no_data_not_as_zero(executor):
    llm = ScriptedLLM([
        AgentStep(
            action="run_view",
            view_name="vw_q002_top_10_customers_by_lifetime_revenue",
            filters={"customer_name": "NOBODY LTD"},
        ),
        AgentStep(action="answer", text="No record of that customer."),
    ])
    await _agent(executor, llm).run("how is NOBODY LTD doing?")

    observation = llm.step_prompts[1].user
    assert "NO MATCHING ROWS" in observation
    assert "Do not infer anything" in observation


async def test_observations_carry_totals_over_every_row(tmp_path):
    """`summarise()` runs over the full result set, so the model narrates
    totals instead of describing whichever rows fit the payload cap."""
    from tests.fixtures.fixture_db import build_fixture_db

    path = build_fixture_db(tmp_path / "wide.db", extra_sales_rows=40)
    executor = ReadOnlyExecutor(path, query_timeout_s=5, max_rows=500)
    try:
        llm = ScriptedLLM([
            AgentStep(action="run_view", view_name="fact_ai_sales_net"),
            AgentStep(action="answer", text="done"),
        ])
        await _agent(executor, llm).run("show me the sales lines")
        observation = llm.step_prompts[1].user
        assert "Totals over all" in observation
        assert "net_revenue: total" in observation
    finally:
        executor.close()


async def test_glossary_is_a_lookup_the_model_chooses_to_make(executor):
    llm = ScriptedLLM([
        AgentStep(action="glossary", query="dead stock"),
        AgentStep(action="answer", text="done"),
    ])
    await _agent(executor, llm).run("talk me about dead stock")

    assert "no sales for 12+ months" in llm.step_prompts[1].user


# ------------------------------------------------------------------- provenance


async def test_trace_flags_sources_whose_formula_is_not_signed_off(executor):
    llm = ScriptedLLM([
        AgentStep(action="run_view",
                  view_name="vw_q011_items_dead_stock_or_severe_dead_stock"),
        AgentStep(action="answer", text="done"),
    ])
    _, trace = await _agent(executor, llm).run("dead stock")

    assert trace.calls[0].assumption_status == "DATA_SCIENCE_REVIEW_REQUIRED"
    assert "vw_q011_items_dead_stock_or_severe_dead_stock" in trace.unvalidated


async def test_trace_lists_every_governed_view_in_order(executor):
    llm = ScriptedLLM([
        AgentStep(action="run_view", view_name="vw_q002_top_10_customers_by_lifetime_revenue"),
        AgentStep(action="run_view", view_name="vw_ai_sales_by_year"),
        AgentStep(action="answer", text="done"),
    ])
    _, trace = await _agent(executor, llm).run("revenue picture")

    assert trace.views_used == [
        "vw_q002_top_10_customers_by_lifetime_revenue",
        "vw_ai_sales_by_year",
    ]


async def test_figures_are_verified_against_every_result_in_the_turn(executor):
    """A composed answer legitimately mixes results; verifying against only
    the last one would flag correct figures as fabricated."""
    settings = Settings(_env_file=None, verification_strict=True)
    llm = ScriptedLLM([
        AgentStep(action="run_view", view_name="vw_q002_top_10_customers_by_lifetime_revenue"),
        AgentStep(action="run_view", view_name="vw_ai_sales_by_year"),
        # 7,600 comes from the FIRST result, not the last.
        AgentStep(action="answer", text="Alpha Trading totals AED 7,600 lifetime."),
    ])
    answer, _ = await _agent(executor, llm, settings).run("top customer?")

    assert "could not be traced" not in answer


async def test_a_fabricated_figure_is_caught_in_strict_mode(executor):
    settings = Settings(_env_file=None, verification_strict=True)
    llm = ScriptedLLM([
        AgentStep(action="run_view", view_name="vw_q002_top_10_customers_by_lifetime_revenue"),
        AgentStep(action="answer", text="Revenue reached AED 4,182,993 last year."),
    ])
    answer, _ = await _agent(executor, llm, settings).run("revenue?")

    assert "could not be traced" in answer


# ------------------------------------------------- parity with a known playbook


@pytest.fixture()
def playbook_db(tmp_path):
    """A database carrying investigate_customer's views, so the loop and the
    playbook can be run against identical data."""
    path = tmp_path / "parity.db"
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE book(customer_name TEXT, customer_code TEXT, revenue REAL, "
        "margin_pct REAL, overdue REAL, invoices REAL, days_since_last REAL)"
    )
    conn.executemany(
        "INSERT INTO book VALUES (?,?,?,?,?,?,?)",
        [
            ("MERSIN TRADE", "C00594", 5_674_262.0, 0.20, 412_900.0, 84.0, 12.0),
            ("HALA CAR CO", "C00075", 4_469_733.0, 0.31, 0.0, 41.0, 5.0),
        ],
    )
    for view in (
        "vw_q002_top_10_customers_by_lifetime_revenue",
        "vw_margin_customer_profitability",
        "vw_q076_customers_outstanding_balances_aging_buckets",
        "vw_q009_customers_highest_invoice_frequency",
        "vw_q007_customers_longest_since_last_order",
        "vw_q003_customers_at_risk_or_inactive",
    ):
        conn.execute(f"CREATE VIEW {view} AS SELECT * FROM book")
    conn.commit()
    conn.close()
    executor = ReadOnlyExecutor(path, query_timeout_s=10, max_rows=500)
    yield executor
    executor.close()


async def test_loop_reaches_the_same_views_the_playbook_uses(playbook_db):
    """Ground truth: investigate_customer names six views a human chose. Given
    the same budget, the loop should land on those same governed objects —
    not on ad-hoc SQL over the base tables."""
    playbook = PlaybookLibrary.load().get("investigate_customer")
    expected = {step.view for step in playbook.steps}

    settings = Settings(_env_file=None, agent_max_steps=6)
    llm = ScriptedLLM([
        AgentStep(action="run_view",
                  view_name="vw_q002_top_10_customers_by_lifetime_revenue"),
        AgentStep(action="run_view", view_name="vw_margin_customer_profitability",
                  filters={"customer_name": "MERSIN TRADE"}),
        AgentStep(action="run_view",
                  view_name="vw_q076_customers_outstanding_balances_aging_buckets",
                  filters={"customer_name": "MERSIN TRADE"}),
        AgentStep(action="run_view",
                  view_name="vw_q003_customers_at_risk_or_inactive",
                  filters={"customer_name": "MERSIN TRADE"}),
        AgentStep(action="answer",
                  text="MERSIN TRADE is worth AED 5,674,262 but carries "
                       "AED 412,900 overdue."),
    ])
    answer, trace = await _agent(playbook_db, llm, settings).run(
        "investigate MERSIN TRADE"
    )

    assert set(trace.views_used) <= expected, "loop left the playbook's governed set"
    assert trace.stopped_because == "answered"
    assert "AED 5,674,262" in answer


async def test_loop_costs_fewer_steps_than_the_playbook_has(playbook_db):
    """The playbook runs all six steps every time. The loop stops when it has
    enough — which is the point of paying for reasoning."""
    playbook = PlaybookLibrary.load().get("investigate_customer")
    settings = Settings(_env_file=None, agent_max_steps=6)
    llm = ScriptedLLM([
        AgentStep(action="run_view", view_name="vw_margin_customer_profitability",
                  filters={"customer_name": "HALA CAR CO"}),
        AgentStep(action="run_view",
                  view_name="vw_q076_customers_outstanding_balances_aging_buckets",
                  filters={"customer_name": "HALA CAR CO"}),
        AgentStep(action="answer",
                  text="HALA CAR CO runs a 31% margin with nothing overdue."),
    ])
    _, trace = await _agent(playbook_db, llm, settings).run(
        "is HALA CAR CO paying on time?"
    )

    assert len(trace.calls) < len(playbook.steps)
    assert trace.stopped_because == "answered"


# ------------------------------------------ regression: fractions written as %


async def test_a_stored_fraction_written_as_a_percentage_is_grounded(playbook_db):
    """margin_pct is stored 0.20; the narrative says "20%". Before the fix both
    percentages in a two-customer comparison were flagged ungrounded, which is
    the false-alarm class that trains people to ignore the verifier."""
    settings = Settings(_env_file=None, verification_strict=True)
    llm = ScriptedLLM([
        AgentStep(action="run_view", view_name="vw_margin_customer_profitability"),
        AgentStep(action="answer",
                  text="MERSIN TRADE runs 20% margin against HALA CAR CO's 31%."),
    ])
    answer, _ = await _agent(playbook_db, llm, settings).run("compare margins")

    assert "could not be traced" not in answer


async def test_a_percentage_with_no_basis_in_the_data_is_still_caught(playbook_db):
    settings = Settings(_env_file=None, verification_strict=True)
    llm = ScriptedLLM([
        AgentStep(action="run_view", view_name="vw_margin_customer_profitability"),
        AgentStep(action="answer", text="Margins average 47.3% across the book."),
    ])
    answer, _ = await _agent(playbook_db, llm, settings).run("average margin?")

    assert "could not be traced" in answer


# ---------------------------------------------------- use_previous_result


async def test_use_previous_result_refines_this_turns_own_observation(executor):
    """Index 0 is this turn's most recent tool result — the loop shouldn't
    need a second run_view just to re-sort what step 1 already fetched."""
    llm = ScriptedLLM([
        AgentStep(action="run_view",
                  view_name="vw_q002_top_10_customers_by_lifetime_revenue"),
        AgentStep(action="use_previous_result", refine_target=0,
                  refine={"sort_by": "customer_name", "limit": 1}),
        AgentStep(action="answer", text="done"),
    ])
    _, trace = await _agent(executor, llm).run("top customer alphabetically")

    assert trace.calls[1].action == "use_previous_result"
    assert trace.calls[1].status == "ok"
    # row_count is the pre-limit filtered count (matches apply_refinement's
    # own contract); the limit is reflected in how many rows come back.
    assert trace.calls[1].row_count == 2


async def test_use_previous_result_reaches_into_the_session_working_set(executor):
    """Index space: this turn's own results come first, then the session's
    working set — so an empty turn can still refine what an EARLIER turn
    fetched, which is the whole point of carrying the working set in."""
    from app.core.models import QueryResult
    from app.orchestrator.conversation import ResultEntry

    prior = ResultEntry(
        label="top customers last turn",
        result=QueryResult(
            columns=["customer_name", "revenue"],
            rows=[("Alpha", 100.0), ("Beta", 50.0)],
            row_count=2, truncated=False, source="view",
            object_name="vw_prior", elapsed_ms=3,
        ),
        source="vw_prior",
    )
    llm = ScriptedLLM([
        AgentStep(action="use_previous_result", refine_target=0,
                  refine={"sort_by": "revenue", "sort_desc": True, "limit": 1}),
        AgentStep(action="answer", text="Alpha leads at 100."),
    ])
    _, trace = await _agent(executor, llm).run(
        "who was on top?", working_set=[prior]
    )

    assert trace.calls[0].status == "ok"
    assert "Alpha" in llm.step_prompts[1].user


async def test_use_previous_result_out_of_range_is_reported_not_crashed(executor):
    llm = ScriptedLLM([
        AgentStep(action="use_previous_result", refine_target=5, refine={}),
        AgentStep(action="answer", text="Started fresh instead."),
    ])
    answer, trace = await _agent(executor, llm).run("refine that")

    assert trace.calls[0].status == "empty"
    assert answer == "Started fresh instead."


async def test_use_previous_result_unknown_column_is_rejected(executor):
    llm = ScriptedLLM([
        AgentStep(action="run_view",
                  view_name="vw_q002_top_10_customers_by_lifetime_revenue"),
        AgentStep(action="use_previous_result", refine_target=0,
                  refine={"sort_by": "not_a_real_column"}),
        AgentStep(action="answer", text="Recovered."),
    ])
    _, trace = await _agent(executor, llm).run("sort by nonsense")

    assert trace.calls[1].status == "rejected"
    assert "not_a_real_column" in trace.calls[1].detail


# ------------------------------------------- agent_step prompt contract


async def test_use_previous_result_is_in_the_json_schema_the_model_sees():
    """Regression pin for a real bug: the tool was documented in the prose
    tool list but missing from the JSON schema's action enum. Since this is
    plain prompted JSON with NO native schema enforcement (structured_call
    just parses returned text), that omission made the tool effectively
    unreachable — the model was never told it was a valid choice, not just
    unlikely to pick it."""
    prompt = PromptManager().render(
        "agent_step", data_as_of="2026-06-27", message="x", history=[],
        observations=[], step=1, steps_remaining=3,
    )
    assert '"use_previous_result"' in prompt.system
    assert "refine_target" in prompt.system


async def test_comparison_strategy_rule_present():
    """Pin for the rule addressing a recurring production failure class:
    generated SQL joining/unioning two periods in one query (FULL OUTER
    JOIN, mismatched UNION columns) hit production twice."""
    prompt = PromptManager().render(
        "agent_step", data_as_of="2026-06-27", message="x", history=[],
        observations=[], step=1, steps_remaining=3,
    )
    system = " ".join(prompt.system.split())
    assert "TWO SIMPLE calls over one complex one" in system


async def test_causal_attribution_rule_present():
    prompt = PromptManager().render(
        "agent_step", data_as_of="2026-06-27", message="x", history=[],
        observations=[], step=1, steps_remaining=3,
    )
    system = " ".join(prompt.system.split())
    assert "quantify each candidate cause's share" in system
    assert "data artifact" in system
