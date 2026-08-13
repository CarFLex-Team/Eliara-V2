from app.api.v1.compat import infer_domain


def test_domain_inference():
    assert infer_domain("vw_q002_top_10_customers_by_lifetime_revenue", False) == "customer"
    assert infer_domain("vw_q011_items_dead_stock_or_severe_dead_stock", False) == "dead_stock"
    assert infer_domain("vw_margin_by_item", False) == "margin"
    assert infer_domain(None, True) == "custom"
    assert infer_domain(None, False) == "general"


async def test_compat_shape_matches_legacy_contract(app_client):
    response = await app_client.post(
        "/api/v1/chat/compat", json={"company_id": "beta", "session_id": "legacy1", "message": "top customers?"}
    )
    assert response.status_code == 200
    body = response.json()

    # exact legacy keys the frontend consumes
    assert set(body) == {
        "answer", "domain", "detail", "endpoint_used", "visual",
        "session_context", "timings",
    }
    assert body["detail"] == body["answer"]
    assert body["visual"] is None
    assert body["domain"] == "customer"
    assert body["endpoint_used"] == "vw_q002_top_10_customers_by_lifetime_revenue"
    assert body["session_context"] == {"last_topic": "customer"}
    stages = [s["stage"] for s in body["timings"]["stages"]]
    assert stages == ["intent routing", "query execution", "answer narration"]
    assert body["timings"]["total_ms"] >= 0
