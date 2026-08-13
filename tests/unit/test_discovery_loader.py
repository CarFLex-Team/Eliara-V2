import pytest

from app.discovery.metadata_loader import MetadataLoader
from app.execution.executor import ReadOnlyExecutor
from tests.fixtures.fixture_db import build_fixture_db


@pytest.fixture()
def loaded(executor):
    return MetadataLoader(executor).load()


def test_eligible_objects_indexed(loaded):
    objects, _, _, _ = loaded
    assert "fact_ai_sales_net" in objects
    assert "dim_b3_item" in objects
    assert "vw_q002_top_10_customers_by_lifetime_revenue" in objects
    assert objects["vw_q002_top_10_customers_by_lifetime_revenue"].category == "question_view"
    assert objects["vw_ai_sales_by_year"].category == "ai_view"


def test_governance_and_sap_layers_excluded(loaded):
    objects, _, _, _ = loaded
    assert "batch_13_business_glossary" not in objects
    assert "batch_09_import_evidence" not in objects
    assert "sap_oitm_raw" not in objects
    assert "chatbot_question_view_registry" not in objects  # metadata, not analytics


def test_columns_loaded(loaded):
    objects, _, _, _ = loaded
    assert "net_revenue" in objects["fact_ai_sales_net"].columns
    assert "customer_code" in objects["vw_q002_top_10_customers_by_lifetime_revenue"].columns


def test_registry_linked_to_views(loaded):
    objects, registry, _, _ = loaded
    assert len(registry) == 3
    q2 = objects["vw_q002_top_10_customers_by_lifetime_revenue"].registry
    assert q2 is not None and q2.question_id == 2
    assert q2.assumption_status == "APPROVED_LOGIC"


def test_glossary_loaded(loaded):
    _, _, glossary, _ = loaded
    assert glossary["dead stock"].startswith("Items with no sales")


def test_fingerprint_changes_with_schema(tmp_path, executor, loaded):
    _, _, _, fp1 = loaded
    import sqlite3

    db2 = tmp_path / "changed.db"
    build_fixture_db(db2)
    conn = sqlite3.connect(db2)
    conn.execute("CREATE VIEW vw_extra AS SELECT 1 AS one")
    conn.commit()
    conn.close()
    ex2 = ReadOnlyExecutor(db2)
    try:
        _, _, _, fp2 = MetadataLoader(ex2).load()
    finally:
        ex2.close()
    assert fp1 != fp2
