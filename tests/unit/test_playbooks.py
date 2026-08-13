"""Playbook engine tests.

A playbook runs several curated views and synthesises them in ONE model call.
The properties that matter: steps are fixed by a human (the model never picks
queries), a missing view degrades rather than fails, and entity-scoped steps
receive the entity while context steps do not.
"""

import sqlite3

import pytest

from app.execution.executor import ReadOnlyExecutor
from app.orchestrator.playbooks import PlaybookLibrary, run_playbook


@pytest.fixture()
def executor(tmp_path):
    path = tmp_path / "pb.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE sales(customer_name TEXT, revenue REAL, margin REAL)")
    conn.executemany(
        "INSERT INTO sales VALUES (?,?,?)",
        [("MERSIN TRADE", 5_674_262.0, 0.20), ("HALA CAR CO", 4_469_733.0, 0.31)],
    )
    conn.execute("CREATE VIEW vw_q002_top_10_customers_by_lifetime_revenue AS SELECT * FROM sales")
    conn.execute("CREATE VIEW vw_margin_customer_profitability AS SELECT * FROM sales")
    conn.execute("CREATE VIEW vw_empty AS SELECT * FROM sales WHERE 1=0")
    conn.commit()
    conn.close()
    ex = ReadOnlyExecutor(path, query_timeout_s=10, max_rows=500)
    yield ex
    ex.close()


@pytest.fixture()
def library():
    return PlaybookLibrary.load()


# ------------------------------------------------------------- definitions


def test_all_shipped_playbooks_are_valid(library):
    assert len(library) == 5
    for name in (
        "investigate_customer", "supplier_review", "stock_action_plan",
        "business_review", "procurement_plan",
    ):
        assert library.get(name) is not None


def test_every_playbook_has_steps_and_synthesis(library):
    for name in ("investigate_customer", "supplier_review", "stock_action_plan"):
        playbook = library.get(name)
        assert playbook.steps
        assert playbook.synthesis.strip()
        assert playbook.triggers


def test_customer_investigation_requires_an_entity(library):
    playbook = library.get("investigate_customer")
    assert playbook.requires_entity
    assert playbook.entity_kind == "customer"
    # Context steps stay unfiltered so the entity can be compared to the portfolio.
    assert any(s.filter_by is None for s in playbook.steps)
    assert any(s.filter_by == "customer_name" for s in playbook.steps)


def test_portfolio_playbooks_need_no_entity(library):
    for name in ("stock_action_plan", "business_review", "procurement_plan"):
        assert not library.get(name).requires_entity


# ----------------------------------------------------------------- running


def test_run_executes_available_steps(executor, library):
    known = {"vw_q002_top_10_customers_by_lifetime_revenue", "vw_margin_customer_profitability"}
    run = run_playbook(library.get("investigate_customer"), executor, known, "MERSIN TRADE")
    assert run.any_data
    assert any(s.status == "ok" for s in run.steps)
    assert "MERSIN TRADE" in run.payload


def test_missing_views_are_skipped_not_fatal(executor, library):
    """Five of the 100 catalogue questions are blocked on unloaded SAP
    sources. A playbook touching one must still produce the rest."""
    known = {"vw_q002_top_10_customers_by_lifetime_revenue"}
    run = run_playbook(library.get("investigate_customer"), executor, known, "MERSIN TRADE")
    assert run.any_data
    missing = [s for s in run.steps if s.status == "missing"]
    assert len(missing) == 5
    assert all(s.note for s in missing)


def test_no_runnable_steps_reports_no_data(executor, library):
    run = run_playbook(library.get("supplier_review"), executor, set(), None)
    assert not run.any_data
    assert all(s.status == "missing" for s in run.steps)


def test_entity_filter_is_applied(executor, library):
    known = {"vw_margin_customer_profitability"}
    run = run_playbook(library.get("investigate_customer"), executor, known, "HALA CAR CO")
    assert "HALA CAR CO" in run.payload
    assert "MERSIN TRADE" not in run.payload


def test_empty_step_is_recorded_without_breaking_the_run(executor, library):
    playbook = library.get("investigate_customer")
    playbook.steps[0].view = "vw_empty"
    run = run_playbook(playbook, executor, {"vw_empty", "vw_margin_customer_profitability"}, "MERSIN TRADE")
    assert any(s.status == "empty" for s in run.steps)
    assert run.any_data


def test_payload_carries_step_labels_and_notes(executor, library):
    known = {"vw_q002_top_10_customers_by_lifetime_revenue"}
    run = run_playbook(library.get("investigate_customer"), executor, known, "MERSIN TRADE")
    assert "### Lifetime value and rank" in run.payload
    assert "Portfolio context" in run.payload


def test_primary_result_is_set_for_charting(executor, library):
    known = {"vw_q002_top_10_customers_by_lifetime_revenue"}
    run = run_playbook(library.get("investigate_customer"), executor, known, None)
    assert run.primary_result is not None
    assert run.primary_result.rows


def test_available_filters_to_this_database(library):
    assert library.available(set()) == []
    partial = library.available({"vw_q012_liquidation_items_highest_capital_locked"})
    assert [p.name for p in partial] == ["stock_action_plan"]


def test_catalogue_reports_runnable_coverage(library):
    entries = library.catalogue({"vw_q012_liquidation_items_highest_capital_locked"})
    stock = next(e for e in entries if e["name"] == "stock_action_plan")
    assert stock["runnable"]
    assert stock["steps_runnable"] == 1
    assert stock["steps_total"] == 7
    assert not next(e for e in entries if e["name"] == "business_review")["runnable"]
