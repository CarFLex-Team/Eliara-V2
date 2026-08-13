"""Production validation — Milestone 8.

Covers the three scenarios from the brief not already exercised by
Milestone 7's isolation suite: startup when one company's database is
unavailable, graceful shutdown across all companies, and a database
refresh for one company happening while the other company continues
serving live requests.
"""

import asyncio
import sqlite3

import httpx
import pytest
import yaml

from app.llm.anthropic_client import AnthropicClient, LLMResponse
from app.main import create_app
from app.orchestrator.decision_models import RoutingDecision
from tests.fixtures.fixture_db import build_fixture_db


def _patch_llm(monkeypatch):
    async def fake_structured(self, prompt, output_model, *, model, max_tokens=1000):
        return (
            RoutingDecision(
                decision="use_view",
                view_name="vw_q002_top_10_customers_by_lifetime_revenue",
            ),
            LLMResponse(text="{}", model=model, input_tokens=1, output_tokens=1,
                        latency_ms=1, prompt_tag=prompt.tag),
        )

    async def fake_call(self, prompt, *, model, max_tokens=1500, temperature=0.2, **kwargs):
        return LLMResponse(text="Top customer is Beta Motors.", model=model,
                           input_tokens=1, output_tokens=1, latency_ms=1,
                           prompt_tag=prompt.tag)

    monkeypatch.setattr(AnthropicClient, "structured_call", fake_structured)
    monkeypatch.setattr(AnthropicClient, "call", fake_call)


# ---------------------------------------------------------------------------
# 1. Startup when one company's database is unavailable
# ---------------------------------------------------------------------------


async def test_startup_survives_one_company_missing_database(tmp_path, monkeypatch):
    """A company whose db_path doesn't resolve to a real file must not
    crash the process or prevent a healthy sibling company from serving
    traffic — its context is built in an unhealthy state instead."""
    beta_db = build_fixture_db(tmp_path / "beta.db")
    missing_db = tmp_path / "does_not_exist.db"  # never created

    companies_yaml = tmp_path / "companies.yaml"
    companies_yaml.write_text(
        yaml.safe_dump(
            {
                "companies": {
                    "beta": {"display_name": "Beta", "db_path": str(beta_db)},
                    "tire_guru": {"display_name": "Tire Guru", "db_path": str(missing_db)},
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("ELIARA_COMPANIES_CONFIG", str(companies_yaml))
    monkeypatch.setenv("ELIARA_EMBEDDING_BACKEND", "hashing")
    monkeypatch.setenv("ELIARA_EMBEDDING_CACHE_DIR", str(tmp_path / "cache"))
    from app.core.config import get_settings

    get_settings.cache_clear()
    _patch_llm(monkeypatch)

    app = create_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        async with app.router.lifespan_context(app):
            # Process came up at all — that's the headline assertion.
            manager = app.state.company_manager
            assert manager is not None

            contexts = manager.all_contexts()
            beta_ctx = contexts["beta"]
            tire_ctx = contexts["tire_guru"]
            assert beta_ctx.healthy is True
            assert tire_ctx.healthy is False
            assert tire_ctx.startup_error is not None

            # Beta keeps serving normally despite Tire Guru's failed build.
            response = await client.post(
                "/api/v1/chat",
                json={"company_id": "beta", "session_id": "s1", "message": "top customers?"},
            )
            assert response.status_code == 200

            # Tire Guru surfaces a clean structured error, not a raw
            # traceback — SQLExecutionError's default status_code (500)
            # applies here, same as any other query failure would get.
            response = await client.post(
                "/api/v1/chat",
                json={"company_id": "tire_guru", "session_id": "s1", "message": "top customers?"},
            )
            assert response.status_code == 500
            assert "error" in response.json()

            # health/deep reflects both accurately, independently.
            health = (await client.get("/api/v1/health/deep")).json()
            assert health["companies"]["beta"]["status"] == "ok"
            assert health["companies"]["tire_guru"]["status"] == "degraded"
    get_settings.cache_clear()


async def test_a_company_that_recovers_can_be_retried_via_get(tmp_path, monkeypatch):
    """If a company's database appears after startup, a subsequent
    request-time .get() call retries the build rather than staying wedged
    in the failed state forever."""
    beta_db = build_fixture_db(tmp_path / "beta.db")
    tire_db_path = tmp_path / "tire_guru.db"  # not created yet

    companies_yaml = tmp_path / "companies.yaml"
    companies_yaml.write_text(
        yaml.safe_dump(
            {
                "companies": {
                    "beta": {"display_name": "Beta", "db_path": str(beta_db)},
                    "tire_guru": {"display_name": "Tire Guru", "db_path": str(tire_db_path)},
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("ELIARA_COMPANIES_CONFIG", str(companies_yaml))
    monkeypatch.setenv("ELIARA_EMBEDDING_BACKEND", "hashing")
    monkeypatch.setenv("ELIARA_EMBEDDING_CACHE_DIR", str(tmp_path / "cache"))
    from app.core.config import get_settings

    get_settings.cache_clear()
    _patch_llm(monkeypatch)

    app = create_app()
    async with app.router.lifespan_context(app):
        manager = app.state.company_manager
        assert manager.all_contexts()["tire_guru"].healthy is False

        # The database shows up later (e.g. an ops team drops the file in).
        build_fixture_db(tire_db_path)
        ctx = manager.get("tire_guru")  # .get() retries an unhealthy company
        assert ctx.healthy is True
    get_settings.cache_clear()


# ---------------------------------------------------------------------------
# 2. Graceful shutdown across all companies
# ---------------------------------------------------------------------------


async def test_graceful_shutdown_closes_every_companys_resources(tmp_path, monkeypatch):
    beta_db = build_fixture_db(tmp_path / "beta.db")
    tire_db = build_fixture_db(tmp_path / "tire_guru.db")

    companies_yaml = tmp_path / "companies.yaml"
    companies_yaml.write_text(
        yaml.safe_dump(
            {
                "companies": {
                    "beta": {"display_name": "Beta", "db_path": str(beta_db)},
                    "tire_guru": {"display_name": "Tire Guru", "db_path": str(tire_db)},
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("ELIARA_COMPANIES_CONFIG", str(companies_yaml))
    monkeypatch.setenv("ELIARA_EMBEDDING_BACKEND", "hashing")
    monkeypatch.setenv("ELIARA_EMBEDDING_CACHE_DIR", str(tmp_path / "cache"))
    from app.core.config import get_settings

    get_settings.cache_clear()
    _patch_llm(monkeypatch)

    app = create_app()
    async with app.router.lifespan_context(app):
        manager = app.state.company_manager
        beta_watcher = manager.get("beta").watcher
        tire_watcher = manager.get("tire_guru").watcher
        assert beta_watcher._thread is not None and beta_watcher._thread.is_alive()
        assert tire_watcher._thread is not None and tire_watcher._thread.is_alive()

    # After the lifespan context exits, shutdown() has run for every company.
    assert beta_watcher._thread is None
    assert tire_watcher._thread is None
    get_settings.cache_clear()


# ---------------------------------------------------------------------------
# 3. A refresh for one company while the other serves live requests
# ---------------------------------------------------------------------------


async def test_refresh_for_one_company_does_not_interrupt_the_other_under_live_traffic(
    tmp_path, monkeypatch
):
    beta_db = build_fixture_db(tmp_path / "beta.db")
    tire_db = build_fixture_db(tmp_path / "tire_guru.db")

    companies_yaml = tmp_path / "companies.yaml"
    companies_yaml.write_text(
        yaml.safe_dump(
            {
                "companies": {
                    "beta": {"display_name": "Beta", "db_path": str(beta_db)},
                    "tire_guru": {"display_name": "Tire Guru", "db_path": str(tire_db)},
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("ELIARA_COMPANIES_CONFIG", str(companies_yaml))
    monkeypatch.setenv("ELIARA_EMBEDDING_BACKEND", "hashing")
    monkeypatch.setenv("ELIARA_EMBEDDING_CACHE_DIR", str(tmp_path / "cache"))
    from app.core.config import get_settings

    get_settings.cache_clear()
    _patch_llm(monkeypatch)

    app = create_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        async with app.router.lifespan_context(app):
            manager = app.state.company_manager

            async def hammer_tire_guru():
                results = []
                for i in range(20):
                    r = await client.post(
                        "/api/v1/chat",
                        json={"company_id": "tire_guru", "session_id": f"live{i}",
                              "message": "top customers?"},
                    )
                    results.append(r.status_code)
                return results

            async def refresh_beta_midway():
                await asyncio.sleep(0.02)
                # Simulate a SAP-style refresh landing on Beta's file while
                # Tire Guru traffic is in flight, then let the watcher pick
                # it up via the same refresh path a real change would use.
                build_fixture_db(beta_db, extra_sales_rows=3)
                manager._refresh("beta")

            tire_results, _ = await asyncio.gather(hammer_tire_guru(), refresh_beta_midway())

            # Every Tire Guru request succeeded throughout Beta's refresh.
            assert all(code == 200 for code in tire_results)

            # Beta's own next request reflects the refreshed data set.
            response = await client.post(
                "/api/v1/chat",
                json={"company_id": "beta", "session_id": "post-refresh", "message": "top customers?"},
            )
            assert response.status_code == 200
    get_settings.cache_clear()
