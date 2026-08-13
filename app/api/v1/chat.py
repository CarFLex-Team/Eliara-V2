"""POST /api/v1/chat — the platform's single user-facing capability."""

import json

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from app.api.deps import get_company_context
from app.api.v1.schemas import ChatRequest, ChatResponse, ResponseMeta
from app.core.config import get_settings
from app.core.errors import EliaraError, RateLimited
from app.detection.attention_queue import AttentionQueue, build_attention_queue

router = APIRouter(tags=["chat"])


class MessageTooLong(EliaraError):
    status_code = 422
    public_message = "The message is too long. Please shorten the question."


@router.post("/chat", response_model=ChatResponse)
async def chat(request: Request, body: ChatRequest) -> ChatResponse:
    ctx = get_company_context(request, body.company_id)
    limiter = getattr(request.app.state, "rate_limiter", None)
    # Namespaced per company so Beta traffic can never exhaust Tire Guru's
    # rate-limit quota (or vice versa) even though the limiter itself is a
    # single shared, platform-level instance.
    rate_key = f"{body.company_id}:{body.session_id}"
    if limiter is not None and not limiter.allow(rate_key):
        raise RateLimited(internal_detail=f"{rate_key} over limit")
    if len(body.message) > get_settings().max_message_chars:
        raise MessageTooLong(internal_detail="message too long")

    outcome = await ctx.orchestrator.handle(body.session_id, body.message)
    metrics = getattr(request.app.state, "metrics", None)
    if metrics is not None:
        metrics["requests_total"] += 1
        if outcome.cache_hit:
            metrics["cache_hits"] += 1
    return ChatResponse(
        answer=outcome.answer,
        meta=ResponseMeta(
            view_used=outcome.view_used,
            sql_generated=outcome.sql_generated,
            cache_hit=outcome.cache_hit,
            latency_ms=outcome.latency_ms,
            input_tokens=outcome.input_tokens,
            output_tokens=outcome.output_tokens,
            source="external" if outcome.decision == "external" else "data",
        ),
    )


@router.post("/chat/stream")
async def chat_stream(request: Request, body: ChatRequest) -> StreamingResponse:
    """Same pipeline as /chat, delivered as Server-Sent Events.

    Each event is `data: <json>\\n\\n`. Event shapes:

      {"type": "stage", "value": "<loading text>"}
      {"type": "token", "value": "<answer text>"}
      {"type": "visual", "value": {...}}   (only when a chart applies)
      {"type": "done"}                     (always last, no payload)
      {"type": "error", "detail": str}     (in place of done, on failure)

    The client renders `token` events as they arrive — EVERY route delivers
    its answer this way, including instant ones (greeting, clarify), so
    there's no separate fallback needed for those. `done` is purely an
    end-of-stream signal; it carries nothing to parse.

    Pre-flight checks (readiness, rate limit, message length) happen BEFORE
    the stream opens, so they still surface as ordinary HTTP error responses
    rather than an error event — a client can tell "never started" from
    "started and failed" by whether it received a 200 with an event stream
    at all.
    """
    ctx = get_company_context(request, body.company_id)
    limiter = getattr(request.app.state, "rate_limiter", None)
    rate_key = f"{body.company_id}:{body.session_id}"
    if limiter is not None and not limiter.allow(rate_key):
        raise RateLimited(internal_detail=f"{rate_key} over limit")
    if len(body.message) > get_settings().max_message_chars:
        raise MessageTooLong(internal_detail="message too long")

    orchestrator = ctx.orchestrator
    metrics = getattr(request.app.state, "metrics", None)

    async def event_source():
        async for event in orchestrator.stream(body.session_id, body.message):
            yield f"data: {json.dumps(event)}\n\n"
        # done carries no payload by design (see docstring), so metrics come
        # from the orchestrator's own record of the turn it just finished —
        # same pattern as _last_result elsewhere in this codebase.
        if metrics is not None:
            outcome = getattr(orchestrator, "_last_outcome", None)
            if outcome is not None:
                metrics["requests_total"] += 1
                if outcome.cache_hit:
                    metrics["cache_hits"] += 1

    return StreamingResponse(
        event_source(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # disable nginx/cloudflared response buffering
        },
    )


class DetectRequest(BaseModel):
    company_id: str
    view_name: str
    value_column: str | None = None
    label_column: str | None = None
    max_items: int = 10


@router.post("/detect")
async def detect(request: Request, body: DetectRequest) -> AttentionQueue:
    """Rank a curated view into a HIGH/MEDIUM/LOW attention queue.

    First slice of proactive monitoring: point this at a view that already
    exists (stock_action_plan's liquidation/dead-stock/replenishment views are
    the natural starting set) and get back a ranked, tiered list instead of
    needing someone to think to ask a question. No LLM call, no fabricated
    confidence score — the ranking is sort-by-the-view's-own-column, tiered
    by quantile, and every item traces back to its source row.

    This is manually triggered, not scheduled. Wiring it to run on a cadence
    (cron, APScheduler, a background task) is the next, separate,
    deployment-specific step — this endpoint is the detection LOGIC, proven
    correct, ready to be called by whatever scheduler gets added.

    view_name must be a real object in the database — the same executor used
    everywhere else runs it, so the same identifier-whitelisting and read-only
    guarantees apply. value_column/label_column are auto-detected from the
    view's own columns when not given; pass them explicitly once you've
    confirmed the real column names for a specific production view.
    """
    ctx = get_company_context(request, body.company_id)

    try:
        result = ctx.executor.run_view(body.view_name, limit=500)
    except EliaraError as exc:
        raise EliaraError(
            internal_detail=exc.internal_detail,
            public_message=f"Could not run {body.view_name!r}: {exc.public_message}",
        ) from exc

    queue = build_attention_queue(
        result, body.view_name,
        value_column=body.value_column, label_column=body.label_column,
        max_items=body.max_items,
    )
    if queue is None:
        raise EliaraError(
            internal_detail=f"{body.view_name}: no usable numeric column to rank by",
            public_message=(
                f"{body.view_name!r} has no rows, or no column that looks like a "
                "number to rank by. Pass value_column/label_column explicitly if "
                "the view has one but it wasn't auto-detected."
            ),
        )
    return queue
