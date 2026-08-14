"""End-to-end tests for the production failures seen in the live transcript.

Each test drives the real orchestrator with a stubbed LLM, so routing, entity
resolution, execution, aggregation and narration all run for real.
"""

import sqlite3

import httpx
import pytest

from app.llm.anthropic_client import AnthropicClient, LLMResponse
from app.main import create_app
from app.orchestrator.decision_models import RoutingDecision
from tests.fixtures.fixture_db import build_fixture_db

# What the routing model produces for "expand more on M. M MANSUR GROUP" —
# title-cased with an extra period. Stored value is "M. M MANSUR GROUP".
MODEL_TYPED_NAME = "M. M. Mansur Group"
STORED_NAME = "M. M MANSUR GROUP"


def _db_with_customers(tmp_path):
    path = build_fixture_db(tmp_path / "e2e.db")
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE dim_b3_customer(customer_code TEXT, customer_name TEXT)")
    conn.executemany(
        "INSERT INTO dim_b3_customer VALUES (?,?)",
        [
            ("C00594", STORED_NAME),
            ("C00075", "CASH CUSTOMER"),
            ("C00472", "D.A.E.Y. ERYON LTD"),
        ],
    )
    conn.execute(
        """CREATE VIEW vw_q005_customer_full_profile_by_code_or_name AS
           SELECT customer_code, customer_name,
                  SUM(net_revenue) AS net_revenue,
                  COUNT(*) AS document_count
           FROM fact_ai_sales_net GROUP BY customer_code, customer_name"""
    )
    # Give Mansur real sales so a successful lookup returns rows.
    conn.execute(
        """INSERT INTO fact_ai_sales_net
           (source_document, posting_date_iso, year, customer_code, customer_name,
            document_number, item_code, item_name, warehouse_code,
            net_quantity, net_revenue, net_gross_profit)
           VALUES ('AR Invoice','2025-05-05','2025','C00594',?,
                   'INV-9','A100','Brake Pad','WH1',5,18568630.38,4589492.0)""",
        (STORED_NAME,),
    )
    # 500 dead-stock items, matching the production result-set size that
    # produced the "likely substantially higher" hedge.
    conn.executemany(
        "INSERT INTO dim_b3_item(item_code, item_name, item_group_code) VALUES (?,?,?)",
        [
            (f"ITEM-{i:04d}", f"SKODA SUPERB HEADLAMP VARIANT {i}", str(600 + i % 5))
            for i in range(500)
        ],
    )
    conn.commit()
    conn.close()
    return path


@pytest.fixture()
async def client(tmp_path, monkeypatch, request):
    db = _db_with_customers(tmp_path)
    from tests.fixtures.company_config import write_companies_yaml

    companies_yaml = write_companies_yaml(tmp_path, db, company_id="beta")
    monkeypatch.setenv("ELIARA_COMPANIES_CONFIG", str(companies_yaml))
    monkeypatch.setenv("ELIARA_DEFAULT_COMPANY_ID", "beta")
    monkeypatch.setenv("ELIARA_EMBEDDING_BACKEND", "hashing")
    monkeypatch.setenv("ELIARA_EMBEDDING_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("ELIARA_AUDIT_ENABLED", "false")
    from app.core.config import get_settings

    get_settings.cache_clear()
    decision = getattr(request, "param", None) or RoutingDecision(
        decision="use_view",
        view_name="vw_q005_customer_full_profile_by_code_or_name",
        endpoint_filters={"customer_name": MODEL_TYPED_NAME},
    )

    async def fake_structured(self, prompt, output_model, *, model, max_tokens=1000):
        return decision, LLMResponse(
            text="{}", model=model, input_tokens=1, output_tokens=1,
            latency_ms=1, prompt_tag=prompt.tag,
        )

    async def fake_call(self, prompt, *, model, max_tokens=1500, temperature=0.2):
        # Echo the rendered prompt so tests can assert what the model SAW.
        return LLMResponse(
            text=f"ANSWER_FROM_PROMPT::{prompt.user}",
            model=model, input_tokens=1, output_tokens=1,
            latency_ms=1, prompt_tag=prompt.tag,
        )

    monkeypatch.setattr(AnthropicClient, "structured_call", fake_structured)
    monkeypatch.setattr(AnthropicClient, "call", fake_call)

    app = create_app()
    transport = httpx.ASGITransport(app=app)
    async with (
        httpx.AsyncClient(transport=transport, base_url="http://t") as c,
        app.router.lifespan_context(app),
    ):
        yield c
    get_settings.cache_clear()


# ------------------------------------------------------------- the Mansur bug


@pytest.mark.anyio
async def test_mansur_lookup_returns_data_not_a_denial(client):
    """Production said "No matching data was found for M. M. Mansur Group"
    about its own third-largest customer. It must now find him."""
    response = await client.post("/ask", json={"message": "expand more on M. M MANSUR GROUP"})
    body = response.json()

    assert body["status"] == "success"
    assert body["row_count"] == 1, "the customer must be found"
    assert STORED_NAME in body["answer"]
    # The prompt the answer model received must contain real rows, not "(empty)".
    assert "(empty result set)" not in body["answer"]


@pytest.mark.anyio
async def test_visual_is_returned_for_the_resolved_customer(client):
    body = (await client.post("/ask", json={"message": "expand on Mansur"})).json()
    assert body["visual"] is not None
    assert body["visual"]["type"] in {"table", "ranking"}


@pytest.mark.parametrize(
    "client",
    [
        RoutingDecision(
            decision="use_view",
            view_name="vw_q005_customer_full_profile_by_code_or_name",
            endpoint_filters={"customer_name": "C00594"},
        )
    ],
    indirect=True,
)
@pytest.mark.anyio
async def test_code_supplied_for_a_name_column_still_resolves(client):
    """"can we expand more on customer C00075" — code in a name filter."""
    body = (await client.post("/ask", json={"message": "expand on C00594"})).json()
    assert body["row_count"] == 1


@pytest.mark.parametrize(
    "client",
    [
        RoutingDecision(
            decision="use_view",
            view_name="vw_q005_customer_full_profile_by_code_or_name",
            endpoint_filters={"customer_name": "Wayne Enterprises"},
        )
    ],
    indirect=True,
)
@pytest.mark.anyio
async def test_truly_unknown_customer_is_told_so_without_querying(client):
    """An unknown entity should get a direct, honest answer — and must not
    burn an LLM call narrating an empty result set."""
    body = (await client.post("/ask", json={"message": "expand on Wayne Enterprises"})).json()
    assert body["status"] == "success"
    assert "could not find" in body["answer"].lower()
    assert "wayne enterprises" in body["answer"].lower()
    assert body["row_count"] == 0


# ------------------------------------------------------------ the stats block


@pytest.mark.parametrize(
    "client",
    [RoutingDecision(decision="use_view", view_name="vw_q011_items_dead_stock_or_severe_dead_stock")],
    indirect=True,
)
@pytest.mark.anyio
async def test_deadstock_answer_prompt_carries_full_set_statistics(client):
    """The answer model must receive totals over every row.

    The stubbed LLM echoes its prompt, so this asserts on exactly what the
    real model would have been given.
    """
    body = (await client.post("/ask", json={"message": "talk me about our deadstock"})).json()
    prompt = body["answer"]
    assert "Full-set statistics" in prompt
    assert "computed over every row" in prompt


@pytest.mark.anyio
async def test_v1_contract_unaffected(client):
    response = await client.post(
        "/api/v1/chat", json={"company_id": "beta", "session_id": "s1", "message": "expand on Mansur"}
    )
    assert response.status_code == 200
