"""Tests for MetadataLoader's registry-table adaptation.

Covers issue #27 — Tire Guru's curated-question registry lives in a table
named ``chatbot_question_router``, with a column set that overlaps with but
doesn't exactly match the canonical ``chatbot_question_view_registry``
schema (confirmed by manual inspection of the real database; the column
names/shapes here are copied from that inspection, not invented).
"""

import sqlite3

import pytest

from app.discovery.metadata_loader import MetadataLoader
from app.execution.executor import ReadOnlyExecutor


@pytest.fixture()
def router_style_executor(tmp_path):
    """A database shaped like Tire Guru's real chatbot_question_router —
    business_status instead of assumption_status/enabled, and
    required_parameters_json instead of requires_endpoint_filter."""
    db = tmp_path / "router.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE chatbot_question_router ("
        "question_id INTEGER PRIMARY KEY, category TEXT, canonical_question TEXT,"
        "view_name TEXT UNIQUE, answer_shape TEXT, required_parameters_json TEXT,"
        "business_status TEXT, response_notes TEXT, anchor_domain TEXT,"
        "relevant_source_tables TEXT, test_focus TEXT)"
    )
    conn.executemany(
        "INSERT INTO chatbot_question_router VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        [
            (1, "Inventory Overview", "How many tire products are in stock?",
             "vw_alpha", "SCALAR", "[]", "READY", "", "INVENTORY", "wholesaler_tires", ""),
            (2, "Inventory Overview", "Which items need a customer filter?",
             "vw_beta", "SCALAR", '["customer_code"]', "DRAFT", "", "INVENTORY", "wholesaler_tires", ""),
        ],
    )
    conn.execute("CREATE VIEW vw_alpha AS SELECT 1 AS one")
    conn.execute("CREATE VIEW vw_beta AS SELECT 2 AS two")
    conn.commit()
    conn.close()
    ex = ReadOnlyExecutor(db)
    yield ex
    ex.close()


def test_adapts_business_status_to_assumption_status(router_style_executor):
    loader = MetadataLoader(router_style_executor, registry_table="chatbot_question_router")
    _, registry, _, _ = loader.load()
    by_id = {e.question_id: e for e in registry}
    assert by_id[1].assumption_status == "READY"
    assert by_id[2].assumption_status == "DRAFT"


def test_derives_enabled_from_business_status_ready(router_style_executor):
    loader = MetadataLoader(router_style_executor, registry_table="chatbot_question_router")
    _, registry, _, _ = loader.load()
    by_id = {e.question_id: e for e in registry}
    assert by_id[1].enabled is True   # READY
    assert by_id[2].enabled is False  # DRAFT


def test_derives_requires_endpoint_filter_from_required_parameters_json(router_style_executor):
    loader = MetadataLoader(router_style_executor, registry_table="chatbot_question_router")
    _, registry, _, _ = loader.load()
    by_id = {e.question_id: e for e in registry}
    assert by_id[1].requires_endpoint_filter is False  # []
    assert by_id[2].requires_endpoint_filter is True   # ["customer_code"]


def test_time_scope_rule_is_none_when_no_equivalent_column(router_style_executor):
    loader = MetadataLoader(router_style_executor, registry_table="chatbot_question_router")
    _, registry, _, _ = loader.load()
    assert all(e.time_scope_rule is None for e in registry)


def test_question_id_canonical_question_view_name_pass_through_unchanged(router_style_executor):
    loader = MetadataLoader(router_style_executor, registry_table="chatbot_question_router")
    _, registry, _, _ = loader.load()
    by_id = {e.question_id: e for e in registry}
    assert by_id[1].canonical_question == "How many tire products are in stock?"
    assert by_id[1].view_name == "vw_alpha"


def test_registry_entries_still_link_to_discovered_objects(router_style_executor):
    loader = MetadataLoader(router_style_executor, registry_table="chatbot_question_router")
    objects, _, _, _ = loader.load()
    assert objects["vw_alpha"].registry is not None
    assert objects["vw_alpha"].registry.question_id == 1


def test_configured_table_missing_degrades_gracefully(tmp_path):
    db = tmp_path / "empty.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE dummy(x TEXT)")
    conn.commit()
    conn.close()
    ex = ReadOnlyExecutor(db)
    try:
        loader = MetadataLoader(ex, registry_table="does_not_exist_here")
        _, registry, _, _ = loader.load()
        assert registry == []
    finally:
        ex.close()


def test_default_registry_table_unchanged_for_backward_compatibility(executor):
    """The default constructor arg must still point at the canonical table
    name, so every existing single-arg MetadataLoader(executor) call site
    keeps working exactly as before."""
    loader = MetadataLoader(executor)
    _, registry, _, _ = loader.load()
    assert len(registry) == 3  # the canonical fixture_db's 3 registry rows
