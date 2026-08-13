"""Legacy-frontend compatibility endpoint.

The existing Eliara frontend consumes the previous platform's response
contract: {answer, domain, detail, endpoint_used, visual, session_context,
timings}. This endpoint translates the v1 pipeline's outcome into that exact
shape. The clean /api/v1/chat contract is untouched.
"""

from fastapi import APIRouter, Request
from pydantic import BaseModel

from app.api.deps import get_company_context
from app.api.v1.chat import MessageTooLong
from app.api.v1.schemas import ChatRequest
from app.core.config import get_settings
from app.core.errors import RateLimited

router = APIRouter(tags=["compat"])

_DOMAIN_KEYWORDS = [
    ("customer", "customer"),
    ("dead", "dead_stock"),
    ("slow_moving", "dead_stock"),
    ("demand", "demand"),
    ("forecast", "demand"),
    ("inventory", "inventory"),
    ("stock", "inventory"),
    ("margin", "margin"),
    ("revenue", "margin"),
    ("profit", "margin"),
    ("sales", "margin"),
    ("procurement", "procurement"),
    ("purchase", "procurement"),
    ("supplier", "supplier"),
]


def infer_domain(view_name: str | None, sql_generated: bool) -> str:
    if view_name:
        lowered = view_name.lower()
        for keyword, domain in _DOMAIN_KEYWORDS:
            if keyword in lowered:
                return domain
    if sql_generated:
        return "custom"
    return "general"


class LegacyTimingStage(BaseModel):
    stage: str
    ms: float


class LegacyTimings(BaseModel):
    total_ms: float
    stages: list[LegacyTimingStage]


class LegacyChatResponse(BaseModel):
    answer: str
    domain: str
    detail: str
    endpoint_used: str
    visual: None = None
    session_context: dict
    timings: LegacyTimings


@router.post("/chat/compat", response_model=LegacyChatResponse)
async def chat_compat(request: Request, body: ChatRequest) -> LegacyChatResponse:
    ctx = get_company_context(request, body.company_id)
    limiter = getattr(request.app.state, "rate_limiter", None)
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

    domain = infer_domain(outcome.view_used, outcome.sql_generated)
    if outcome.view_used:
        endpoint_used = outcome.view_used
    elif outcome.sql_generated:
        endpoint_used = "sql:generated"
    else:
        endpoint_used = f"none:{outcome.decision}"

    return LegacyChatResponse(
        answer=outcome.answer,
        domain=domain,
        detail=outcome.answer,
        endpoint_used=endpoint_used,
        session_context={"last_topic": domain},
        timings=LegacyTimings(
            total_ms=float(outcome.latency_ms),
            stages=[
                LegacyTimingStage(stage="intent routing", ms=float(outcome.routing_ms)),
                LegacyTimingStage(stage="query execution", ms=float(outcome.execution_ms)),
                LegacyTimingStage(stage="answer narration", ms=float(outcome.answer_ms)),
            ],
        ),
    )
