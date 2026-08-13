"""AnthropicClient tests via httpx.MockTransport — no network, no SDK internals."""

import json

import httpx
import pytest
from pydantic import BaseModel

from app.core.config import Settings
from app.core.errors import LLMUnavailableError, RoutingError
from app.llm.anthropic_client import AnthropicClient
from app.prompts.loader import RenderedPrompt

PROMPT = RenderedPrompt(name="t", version=1, system="sys", user="hello")


def _api_response(text: str) -> dict:
    return {
        "id": "msg_1", "type": "message", "role": "assistant",
        "model": "claude-sonnet-4-6",
        "content": [{"type": "text", "text": text}],
        "stop_reason": "end_turn", "stop_sequence": None,
        "usage": {"input_tokens": 10, "output_tokens": 5},
    }


def _client(handler) -> AnthropicClient:
    settings = Settings(_env_file=None, anthropic_api_key="sk-test", llm_max_retries=2)
    transport = httpx.MockTransport(handler)
    return AnthropicClient(settings, http_client=httpx.AsyncClient(transport=transport))


async def test_call_returns_text_and_usage():
    client = _client(lambda req: httpx.Response(200, json=_api_response("hi there")))
    response = await client.call(PROMPT, model="claude-sonnet-4-6")
    assert response.text == "hi there"
    assert response.input_tokens == 10 and response.output_tokens == 5
    assert response.prompt_tag == "t@v1"


async def test_retry_on_429_then_success():
    attempts = {"n": 0}

    def handler(req):
        attempts["n"] += 1
        if attempts["n"] == 1:
            return httpx.Response(429, json={"error": {"type": "rate_limit_error", "message": "slow down"}})
        return httpx.Response(200, json=_api_response("ok"))

    client = _client(handler)
    response = await client.call(PROMPT, model="m")
    assert response.text == "ok"
    assert attempts["n"] == 2


async def test_exhausted_retries_raise_unavailable():
    client = _client(lambda req: httpx.Response(503, json={"error": {"type": "overloaded_error", "message": "x"}}))
    with pytest.raises(LLMUnavailableError):
        await client.call(PROMPT, model="m")


async def test_non_retryable_fails_fast():
    attempts = {"n": 0}

    def handler(req):
        attempts["n"] += 1
        return httpx.Response(400, json={"error": {"type": "invalid_request_error", "message": "bad"}})

    client = _client(handler)
    with pytest.raises(LLMUnavailableError):
        await client.call(PROMPT, model="m")
    assert attempts["n"] == 1


class Decision(BaseModel):
    decision: str
    view_name: str | None = None


async def test_structured_call_parses_fenced_json():
    payload = '```json\n{"decision": "use_view", "view_name": "vw_q002"}\n```'
    client = _client(lambda req: httpx.Response(200, json=_api_response(payload)))
    decision, response = await client.structured_call(PROMPT, Decision, model="m")
    assert decision.decision == "use_view"
    assert decision.view_name == "vw_q002"


async def test_structured_call_corrective_retry():
    attempts = {"n": 0}

    def handler(req):
        attempts["n"] += 1
        body = json.loads(req.content)
        if attempts["n"] == 1:
            return httpx.Response(200, json=_api_response("not json at all"))
        # the corrective turn must include the error feedback
        assert "Correction" in body["messages"][0]["content"]
        return httpx.Response(200, json=_api_response('{"decision": "clarify"}'))

    client = _client(handler)
    decision, _ = await client.structured_call(PROMPT, Decision, model="m")
    assert decision.decision == "clarify"
    assert attempts["n"] == 2


async def test_structured_call_gives_up_with_routing_error():
    client = _client(lambda req: httpx.Response(200, json=_api_response("garbage")))
    with pytest.raises(RoutingError):
        await client.structured_call(PROMPT, Decision, model="m")
