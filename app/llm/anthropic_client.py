"""The only wrapper around the Anthropic SDK.

Responsibilities: retries with exponential backoff (transient errors only),
timeouts, token accounting, and structured JSON calls validated by Pydantic.
Secrets never leave this module; callers pass rendered prompts, never keys.
"""

import asyncio
import json
import time
from typing import TypeVar

import anthropic
from pydantic import BaseModel, ValidationError

from app.core.config import Settings
from app.core.errors import LLMUnavailableError, RoutingError
from app.core.logging import get_logger
from app.prompts.loader import RenderedPrompt

log = get_logger("llm")

T = TypeVar("T", bound=BaseModel)

_RETRYABLE_STATUS = {429, 500, 502, 503, 529}


class LLMResponse(BaseModel):
    text: str
    model: str
    input_tokens: int
    output_tokens: int
    latency_ms: int
    prompt_tag: str


class StreamChunk(BaseModel):
    """One event from AnthropicClient.stream(). Either a text delta (`done`
    False) or the terminal event carrying the same LLMResponse `.call()`
    would have returned (`done` True, `text` empty)."""

    text: str = ""
    done: bool = False
    response: LLMResponse | None = None


class AnthropicClient:
    def __init__(self, settings: Settings, http_client=None) -> None:
        self._settings = settings
        self._client = anthropic.AsyncAnthropic(
            api_key=settings.anthropic_api_key.get_secret_value() or "missing",
            timeout=settings.llm_timeout_s,
            max_retries=0,  # we own the retry policy
            http_client=http_client,
        )

    async def call(
        self,
        prompt: RenderedPrompt,
        *,
        model: str,
        max_tokens: int = 1500,
        temperature: float = 0.2,
        web_search: bool = False,
    ) -> LLMResponse:
        attempts = self._settings.llm_max_retries + 1
        last_error: Exception | None = None
        for attempt in range(attempts):
            start = time.perf_counter()
            try:
                message = await self._client.messages.create(
                    model=model,
                    max_tokens=max_tokens,
                    temperature=temperature,
                    system=prompt.system or anthropic.NOT_GIVEN,
                    messages=[{"role": "user", "content": prompt.user}],
                    tools=(
                        [{"type": "web_search_20250305", "name": "web_search"}]
                        if web_search else anthropic.NOT_GIVEN
                    ),
                )
                response = LLMResponse(
                    # Only text blocks. With web search on, the response also
                    # carries server_tool_use and web_search_tool_result blocks
                    # — machinery, not answer.
                    text="".join(b.text for b in message.content if b.type == "text"),
                    model=model,
                    input_tokens=message.usage.input_tokens,
                    output_tokens=message.usage.output_tokens,
                    latency_ms=int((time.perf_counter() - start) * 1000),
                    prompt_tag=prompt.tag,
                )
                log.info(
                    "llm_call",
                    prompt=prompt.tag,
                    model=model,
                    input_tokens=response.input_tokens,
                    output_tokens=response.output_tokens,
                    latency_ms=response.latency_ms,
                    attempt=attempt + 1,
                )
                return response
            except anthropic.APIStatusError as exc:
                last_error = exc
                if exc.status_code not in _RETRYABLE_STATUS:
                    raise LLMUnavailableError(
                        internal_detail=f"{prompt.tag}: non-retryable {exc.status_code}"
                    ) from exc
            except (anthropic.APIConnectionError, anthropic.APITimeoutError) as exc:
                last_error = exc
            if attempt < attempts - 1:
                await asyncio.sleep(0.5 * (2**attempt))
        raise LLMUnavailableError(
            internal_detail=f"{prompt.tag}: exhausted {attempts} attempts ({last_error})"
        ) from last_error

    async def stream(
        self,
        prompt: RenderedPrompt,
        *,
        model: str,
        max_tokens: int = 1500,
        temperature: float = 0.2,
        web_search: bool = False,
        max_searches: int = 5,
    ):
        """Token-by-token delivery for free-text answer calls.

        Deliberately NOT used for structured_call — a routing or agent-step
        decision is ~40 tokens of JSON; streaming it saves nothing and
        partial JSON can't be acted on until it's complete anyway. This is
        for the calls where the user is actually waiting on prose: the
        answer narration, the playbook synthesis, the agent's forced landing.

        No retry policy here, unlike `.call()`. A stream that fails partway
        through has already sent partial text to the user; retrying would
        either duplicate it or require the caller to discard and restart the
        whole response. Callers needing resilience should fall back to
        `.call()` on a `LLMUnavailableError` from the first chunk.
        """
        start = time.perf_counter()
        text_parts: list[str] = []
        # Web search is opt-in per call and used ONLY by the external-knowledge
        # path. The analytics paths must never get it: their whole guarantee is
        # that every figure traces to a governed view, and a model that can
        # reach the open web could quietly blend outside numbers into an answer
        # that looks internally sourced.
        tools = (
            [{"type": "web_search_20250305", "name": "web_search",
              "max_uses": max_searches}]
            if web_search else anthropic.NOT_GIVEN
        )
        try:
            async with self._client.messages.stream(
                model=model,
                max_tokens=max_tokens,
                temperature=temperature,
                system=prompt.system or anthropic.NOT_GIVEN,
                messages=[{"role": "user", "content": prompt.user}],
                tools=tools,
            ) as stream:
                async for text in stream.text_stream:
                    text_parts.append(text)
                    yield StreamChunk(text=text)
                final_message = await stream.get_final_message()
        except anthropic.APIStatusError as exc:
            raise LLMUnavailableError(
                internal_detail=f"{prompt.tag}: stream failed with {exc.status_code}"
            ) from exc
        except (anthropic.APIConnectionError, anthropic.APITimeoutError) as exc:
            raise LLMUnavailableError(
                internal_detail=f"{prompt.tag}: stream connection failed"
            ) from exc

        response = LLMResponse(
            text="".join(text_parts),
            model=model,
            input_tokens=final_message.usage.input_tokens,
            output_tokens=final_message.usage.output_tokens,
            latency_ms=int((time.perf_counter() - start) * 1000),
            prompt_tag=prompt.tag,
        )
        log.info(
            "llm_stream_call",
            prompt=prompt.tag,
            model=model,
            input_tokens=response.input_tokens,
            output_tokens=response.output_tokens,
            latency_ms=response.latency_ms,
        )
        yield StreamChunk(done=True, response=response)

    async def structured_call(
        self,
        prompt: RenderedPrompt,
        output_model: type[T],
        *,
        model: str,
        max_tokens: int = 1000,
    ) -> tuple[T, LLMResponse]:
        """Call expecting strict JSON; one corrective retry on parse failure."""
        response = await self.call(prompt, model=model, max_tokens=max_tokens, temperature=0.0)
        try:
            return self._parse(response.text, output_model), response
        except (json.JSONDecodeError, ValidationError) as first_error:
            corrective = RenderedPrompt(
                name=prompt.name,
                version=prompt.version,
                system=prompt.system,
                user=(
                    f"{prompt.user}\n\n## Correction\nYour previous output was invalid: "
                    f"{first_error}\nReturn ONLY the corrected JSON object."
                ),
            )
            response = await self.call(corrective, model=model, max_tokens=max_tokens, temperature=0.0)
            try:
                return self._parse(response.text, output_model), response
            except (json.JSONDecodeError, ValidationError) as exc:
                raise RoutingError(
                    internal_detail=f"{prompt.tag}: invalid JSON after retry: {exc}"
                ) from exc

    @staticmethod
    def _parse(text: str, output_model: type[T]) -> T:
        cleaned = text.strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.strip("`")
            cleaned = cleaned.removeprefix("json")
        start, end = cleaned.find("{"), cleaned.rfind("}")
        if start == -1 or end == -1:
            raise json.JSONDecodeError("no JSON object found", cleaned, 0)
        return output_model.model_validate(json.loads(cleaned[start : end + 1]))
