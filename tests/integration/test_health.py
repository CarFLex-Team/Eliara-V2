import httpx
import pytest

from app.main import create_app


@pytest.fixture
async def client(tmp_path, monkeypatch):
    # No companies.yaml registered at all — company_manager stays None,
    # matching the original "nothing built yet" state this test targets.
    monkeypatch.setenv("ELIARA_COMPANIES_CONFIG", str(tmp_path / "does-not-exist.yaml"))
    from app.core.config import get_settings

    get_settings.cache_clear()
    app = create_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        async with app.router.lifespan_context(app):
            yield c
    get_settings.cache_clear()


async def test_health_ok(client):
    r = await client.get("/api/v1/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"
    assert "X-Request-ID" in r.headers


async def test_deep_health_reports_m0_state(client):
    r = await client.get("/api/v1/health/deep")
    assert r.status_code == 200
    body = r.json()
    assert body["metadata_index"] == "not_built"


async def test_unknown_route_is_clean_404(client):
    r = await client.get("/api/v1/nope")
    assert r.status_code == 404


async def test_deep_health_reports_db_when_configured(tmp_path, monkeypatch):
    from tests.fixtures.company_config import write_companies_yaml
    from tests.fixtures.fixture_db import build_fixture_db

    db = build_fixture_db(tmp_path / "h.db")
    companies_yaml = write_companies_yaml(tmp_path, db, company_id="beta")
    monkeypatch.setenv("ELIARA_COMPANIES_CONFIG", str(companies_yaml))
    from app.core.config import get_settings

    get_settings.cache_clear()
    app = create_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        async with app.router.lifespan_context(app):
            r = await c.get("/api/v1/health/deep?company_id=beta")
    get_settings.cache_clear()
    body = r.json()
    assert body["database"].startswith("ok")
