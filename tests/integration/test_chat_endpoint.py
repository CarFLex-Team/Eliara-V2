COMPANY_ID = "beta"


async def test_chat_end_to_end(app_client):
    response = await app_client.post(
        "/api/v1/chat", json={"company_id": COMPANY_ID, "session_id": "s1", "message": "top customers?"}
    )
    assert response.status_code == 200
    body = response.json()
    assert "Beta Motors" in body["answer"]
    assert body["meta"]["view_used"] == "vw_q002_top_10_customers_by_lifetime_revenue"
    assert body["meta"]["sql_generated"] is False
    assert body["meta"]["source"] == "data"


async def test_chat_unknown_company_is_404(app_client):
    response = await app_client.post(
        "/api/v1/chat",
        json={"company_id": "not_registered", "session_id": "s1", "message": "top customers?"},
    )
    assert response.status_code == 404


async def test_search_prefix_returns_external_source(app_client):
    response = await app_client.post(
        "/api/v1/chat",
        json={"company_id": COMPANY_ID, "session_id": "s1", "message": "/search headlamp makers"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["answer"]
    assert body["meta"]["source"] == "external"
    assert body["meta"]["view_used"] is None
    assert body["meta"]["sql_generated"] is False


async def test_chat_message_too_long(app_client):
    response = await app_client.post(
        "/api/v1/chat", json={"company_id": COMPANY_ID, "session_id": "s1", "message": "x" * 5000}
    )
    assert response.status_code == 422
    assert "too long" in response.json()["error"]


async def test_session_history_endpoint(app_client):
    await app_client.post(
        "/api/v1/chat", json={"company_id": COMPANY_ID, "session_id": "hist1", "message": "top customers?"}
    )
    response = await app_client.get(f"/api/v1/sessions/hist1?company_id={COMPANY_ID}")
    assert response.status_code == 200
    messages = response.json()["messages"]
    assert len(messages) == 2
    assert messages[0]["role"] == "user"
    assert messages[1]["role"] == "assistant"


async def test_unknown_session_returns_empty(app_client):
    response = await app_client.get(f"/api/v1/sessions/never-seen?company_id={COMPANY_ID}")
    assert response.json()["messages"] == []


async def test_rate_limit_429(app_client):
    from httpx import ASGITransport

    from app.core.cache import RateLimiter

    transport = app_client._transport
    assert isinstance(transport, ASGITransport)
    transport.app.state.rate_limiter = RateLimiter(max_per_minute=2)

    async def _post():
        return await app_client.post(
            "/api/v1/chat", json={"company_id": COMPANY_ID, "session_id": "burst", "message": "top customers?"}
        )

    codes = [(await _post()).status_code for _ in range(4)]
    assert codes.count(429) >= 1
    # other sessions unaffected
    other = await app_client.post(
        "/api/v1/chat", json={"company_id": COMPANY_ID, "session_id": "calm", "message": "top customers?"}
    )
    assert other.status_code == 200


async def test_concurrent_requests_all_succeed(app_client):
    import asyncio

    async def one(i: int):
        return await app_client.post(
            "/api/v1/chat", json={"company_id": COMPANY_ID, "session_id": f"c{i}", "message": "top customers?"}
        )

    responses = await asyncio.gather(*[one(i) for i in range(10)])
    assert all(r.status_code == 200 for r in responses)


async def test_deep_health_exposes_counters(app_client):
    await app_client.post(
        "/api/v1/chat", json={"company_id": COMPANY_ID, "session_id": "m1", "message": "top customers?"}
    )
    response = await app_client.get("/api/v1/health/deep")
    body = response.json()
    assert body["requests_total"] >= 1
    assert "cache_hits" in body
    assert COMPANY_ID in body["companies"]
    assert body["companies"][COMPANY_ID]["status"] == "ok"


async def test_deep_health_single_company_shape(app_client):
    response = await app_client.get(f"/api/v1/health/deep?company_id={COMPANY_ID}")
    body = response.json()
    assert body["status"] == "ok"
    assert body["database"].startswith("ok")


# ------------------------------------------------------------- /detect


async def test_detect_ranks_a_real_view(app_client):
    response = await app_client.post(
        "/api/v1/detect",
        json={"company_id": COMPANY_ID, "view_name": "vw_q002_top_10_customers_by_lifetime_revenue"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["view_name"] == "vw_q002_top_10_customers_by_lifetime_revenue"
    assert len(body["items"]) > 0
    assert body["items"][0]["tier"] in {"HIGH", "MEDIUM", "LOW"}
    # descending by value
    values = [item["value"] for item in body["items"]]
    assert values == sorted(values, reverse=True)


async def test_detect_unknown_view_is_a_clear_error_not_a_500_leak(app_client):
    response = await app_client.post(
        "/api/v1/detect", json={"company_id": COMPANY_ID, "view_name": "not_a_real_view"}
    )
    assert response.status_code >= 400
    body = response.json()
    assert "not_a_real_view" in body["error"]
    assert "sqlite" not in body["error"].lower()  # no internal detail leaked


async def test_detect_respects_max_items(app_client):
    response = await app_client.post(
        "/api/v1/detect",
        json={
            "company_id": COMPANY_ID,
            "view_name": "vw_q002_top_10_customers_by_lifetime_revenue",
            "max_items": 1,
        },
    )
    body = response.json()
    assert len(body["items"]) == 1
