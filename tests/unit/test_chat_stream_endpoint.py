"""POST /api/v1/chat/stream — SSE framing and error-shape tests.

Deliberately not booting the full app.lifespan (real DB, discovery, LLM
client). The endpoint's own job is narrow: pre-flight checks stay ordinary
HTTP responses, and orchestrator.stream() events get framed as
`data: <json>\n\n`. A fake company_manager/context with a scripted
orchestrator.stream() is enough to test exactly that, without dragging in
the whole stack.
"""

import json
from dataclasses import dataclass

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from app.api.v1.chat import router as chat_router
from app.core.errors import EliaraError

COMPANY_ID = "beta"


class FakeStreamingOrchestrator:
    def __init__(self, events: list[dict], last_outcome=None):
        self._events = events
        self._last_outcome = last_outcome

    async def stream(self, session_id: str, message: str):
        for event in self._events:
            yield event

    async def handle(self, session_id: str, message: str):
        raise AssertionError("the streaming endpoint must call .stream(), not .handle()")


@dataclass
class FakeContext:
    orchestrator: object
    healthy: bool = True


class FakeCompanyManager:
    def __init__(self, contexts: dict[str, FakeContext]):
        self._contexts = contexts

    def get(self, company_id: str) -> FakeContext:
        from app.company.registry import UnknownCompany

        try:
            return self._contexts[company_id]
        except KeyError:
            raise UnknownCompany(internal_detail=f"unknown company_id: {company_id!r}") from None


def _register_error_handler(app: FastAPI) -> None:
    """Same handler main.py registers — a domain error becomes its declared
    status code and public_message, not an unhandled 500."""

    @app.exception_handler(EliaraError)
    async def eliara_error_handler(_, exc: EliaraError) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content={"error": exc.public_message})


def _app(events: list[dict], *, rate_limiter=None, last_outcome=None, no_manager=False) -> FastAPI:
    app = FastAPI()
    app.include_router(chat_router, prefix="/api/v1")
    if no_manager:
        app.state.company_manager = None
    else:
        orchestrator = FakeStreamingOrchestrator(events, last_outcome=last_outcome)
        app.state.company_manager = FakeCompanyManager(
            {COMPANY_ID: FakeContext(orchestrator=orchestrator)}
        )
    app.state.rate_limiter = rate_limiter
    app.state.metrics = {"requests_total": 0, "cache_hits": 0}
    _register_error_handler(app)
    return app


def _body(message: str, session_id: str = "s1") -> dict:
    return {"company_id": COMPANY_ID, "session_id": session_id, "message": message}


def _parse_sse(body: str) -> list[dict]:
    events = []
    for block in body.strip().split("\n\n"):
        block = block.strip()
        if not block:
            continue
        assert block.startswith("data: ")
        events.append(json.loads(block[len("data: "):]))
    return events


def test_stream_endpoint_frames_each_event_as_sse_data(monkeypatch):
    scripted = [
        {"type": "stage", "value": "Querying your data..."},
        {"type": "token", "value": "Alpha"},
        {"type": "token", "value": " Trading leads."},
        {"type": "visual", "value": {"type": "ranking", "title": "Top customers", "ranking": []}},
        {"type": "done"},
    ]
    app = _app(scripted)
    with TestClient(app) as client:
        response = client.post("/api/v1/chat/stream", json=_body("top customers?"))

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    events = _parse_sse(response.text)
    assert events == scripted


def test_stream_endpoint_sets_no_cache_and_no_buffering_headers():
    app = _app([{"type": "token", "value": "x"}, {"type": "done"}])
    with TestClient(app) as client:
        response = client.post("/api/v1/chat/stream", json=_body("hi"))
    assert response.headers["cache-control"] == "no-cache"
    assert response.headers["x-accel-buffering"] == "no"


def test_stream_endpoint_rejects_over_limit_before_opening_the_stream(monkeypatch):
    """Pre-flight checks stay ordinary HTTP errors — the client can tell
    "never started" from "started and failed" by whether it got an event
    stream at all."""
    import app.api.v1.chat as chat_module
    from app.core.config import Settings

    monkeypatch.setattr(
        chat_module, "get_settings", lambda: Settings(_env_file=None, max_message_chars=5)
    )
    app = _app([{"type": "token", "value": "unreachable"}, {"type": "done"}])
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/chat/stream",
            json=_body("this message is too long"),
        )
    assert response.status_code == 422
    assert response.headers["content-type"].startswith("application/json")


def test_stream_endpoint_rejects_when_rate_limited():
    class AlwaysDeny:
        def allow(self, key):
            return False

    app = _app(
        [{"type": "token", "value": "unreachable"}, {"type": "done"}],
        rate_limiter=AlwaysDeny(),
    )
    with TestClient(app) as client:
        response = client.post("/api/v1/chat/stream", json=_body("hi"))
    assert response.status_code == 429


def test_stream_endpoint_503s_when_company_manager_not_initialized():
    app = _app([], no_manager=True)
    with TestClient(app) as client:
        response = client.post("/api/v1/chat/stream", json=_body("hi"))
    assert response.status_code == 503


def test_stream_endpoint_404s_for_unknown_company():
    app = _app([{"type": "token", "value": "x"}, {"type": "done"}])
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/chat/stream",
            json={"company_id": "not_registered", "session_id": "s1", "message": "hi"},
        )
    assert response.status_code == 404


def test_stream_endpoint_updates_metrics_from_last_outcome():
    """done carries no payload by design — metrics come from the
    orchestrator's own record of the finished turn instead."""
    from app.orchestrator.orchestrator import ChatOutcome

    scripted = [{"type": "token", "value": "x"}, {"type": "done"}]
    outcome = ChatOutcome(answer="x", decision="use_view", cache_hit=True)
    app = _app(scripted, last_outcome=outcome)
    with TestClient(app) as client:
        client.post("/api/v1/chat/stream", json=_body("hi"))

    assert app.state.metrics["requests_total"] == 1
    assert app.state.metrics["cache_hits"] == 1


def test_stream_endpoint_metrics_skip_cache_hit_when_outcome_says_no():
    from app.orchestrator.orchestrator import ChatOutcome

    scripted = [{"type": "token", "value": "x"}, {"type": "done"}]
    outcome = ChatOutcome(answer="x", decision="use_view", cache_hit=False)
    app = _app(scripted, last_outcome=outcome)
    with TestClient(app) as client:
        client.post("/api/v1/chat/stream", json=_body("hi"))

    assert app.state.metrics["requests_total"] == 1
    assert app.state.metrics["cache_hits"] == 0
