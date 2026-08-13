"""Contract tests against the DEPLOYED frontend (virtual-assistant-master).

Every assertion here mirrors a specific line in the frontend source. If one of
these fails, the live website breaks — regardless of what the API "looks" fine
doing. Source references are noted per test.
"""

import pytest


@pytest.mark.anyio
async def test_ask_accepts_the_exact_frontend_body(app_client):
    """utils/api.ts sends {"message": q} — no session_id, no client_id."""
    response = await app_client.post("/ask", json={"message": "Top customers by revenue?"})
    assert response.status_code == 200


@pytest.mark.anyio
async def test_success_status_is_not_the_string_error(app_client):
    """ChatWindow.tsx: `if (!aiData || aiData.status === "error")` -> ErrorBubble."""
    response = await app_client.post("/ask", json={"message": "Top customers by revenue?"})
    body = response.json()
    assert body["status"] != "error"
    assert body["status"] == "success"


@pytest.mark.anyio
async def test_answer_is_a_plain_string(app_client):
    """ChatWindow.tsx: `content: aiData?.answer` -> FormattedMessage does text.split()."""
    body = (await app_client.post("/ask", json={"message": "Top customers?"})).json()
    assert isinstance(body["answer"], str)
    assert body["answer"]


@pytest.mark.anyio
async def test_visual_is_renderable_or_null(app_client):
    """MessageBubble.tsx dispatches on visual.type.toLowerCase()."""
    body = (await app_client.post("/ask", json={"message": "Top customers?"})).json()
    visual = body["visual"]
    if visual is None:
        return
    assert visual["type"] in {"trend", "table", "ranking", "distribution", "clarification"}
    if visual["type"] == "table":
        # TableView.tsx maps data.columns then indexes each row
        assert isinstance(visual["columns"], list)
        assert isinstance(visual["rows"], list)
        for row in visual["rows"]:
            assert len(row) == len(visual["columns"])
            # `row[i] || row[col] || ""` blanks falsy cells -> everything is a
            # non-empty string so 0 and NULL stay visible
            assert all(isinstance(cell, str) and cell for cell in row)
    if visual["type"] == "ranking":
        # RankingView.tsx reads data.ranking[].label / .value
        assert visual["ranking"]
        for item in visual["ranking"]:
            assert isinstance(item["label"], str)
            assert isinstance(item["value"], (int, float))


@pytest.mark.anyio
async def test_errors_return_http_200_with_error_object(app_client):
    """api.ts throws on !res.ok, so failures must be 200 + status="error",
    and ErrorBubble reads content.code / content.message."""
    response = await app_client.post("/ask", json={"message": "x" * 5000})
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "error"
    assert isinstance(body["error"], dict)
    assert body["error"]["code"]
    assert body["error"]["message"]


@pytest.mark.anyio
async def test_empty_message_does_not_422(app_client):
    """Old QueryRequest defaulted every field; a 422 would surface to the user
    as the generic 'error while fetching' bubble."""
    response = await app_client.post("/ask", json={})
    assert response.status_code == 200
    assert response.json()["status"] == "error"


@pytest.mark.anyio
async def test_cors_preflight_is_allowed(app_client):
    """The browser calls the API cross-origin from the Vercel app."""
    response = await app_client.options(
        "/ask",
        headers={
            "Origin": "https://eliara.vercel.app",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type",
        },
    )
    assert response.status_code in (200, 204)
    assert response.headers.get("access-control-allow-origin")


@pytest.mark.anyio
async def test_same_caller_shares_conversation_history(app_client):
    """The frontend sends no session id; identical callers must still land in
    one session or follow-up resolution silently dies."""
    headers = {"cf-connecting-ip": "203.0.113.9", "user-agent": "Mozilla/5.0 test"}
    await app_client.post("/ask", json={"message": "Top customers?"}, headers=headers)
    await app_client.post("/ask", json={"message": "sort them by margin"}, headers=headers)

    from app.api.legacy import _resolve_session_id

    class _Req:
        def __init__(self, h):
            self.headers = h
            self.client = None

    class _Body:
        session_id = None
        thread_id = None

    session = _resolve_session_id(_Req(headers), _Body())
    history = (await app_client.get(f"/api/v1/sessions/{session}?company_id=beta")).json()
    assert len(history["messages"]) >= 4  # 2 user + 2 assistant


@pytest.mark.anyio
async def test_explicit_session_id_wins_when_frontend_sends_one(app_client):
    await app_client.post(
        "/ask", json={"message": "Top customers?", "session_id": "thread-abc"}
    )
    history = (await app_client.get("/api/v1/sessions/thread-abc?company_id=beta")).json()
    assert len(history["messages"]) == 2


@pytest.mark.anyio
async def test_root_health_matches_old_shape(app_client):
    body = (await app_client.get("/health")).json()
    assert body["status"] in {"ok", "degraded"}
    assert body["v4_engines"]["database"] in {"ready", "not found"}


@pytest.mark.anyio
async def test_v1_chat_contract_still_works(app_client):
    """The clean contract must be untouched by the legacy layer."""
    response = await app_client.post(
        "/api/v1/chat", json={"company_id": "beta", "session_id": "s1", "message": "Top customers?"}
    )
    assert response.status_code == 200
    assert "answer" in response.json()
