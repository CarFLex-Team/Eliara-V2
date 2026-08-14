"""Legacy API surface — matches the deployed Next.js frontend exactly.

Verified against `virtual-assistant-master/utils/api.ts`:

    POST https://api.eliaracarflex.cfd/ask
    headers: Content-Type: application/json, Accept: */*
    body:    {"message": "<question>"}

and against `components/ChatWindow.tsx`, which branches on the response as:

    if (!aiData || aiData.status === "error")  -> ErrorBubble(aiData.error.{code,message})
    else                                        -> content = aiData.answer
                                                   visual  = aiData.visual || null

`visual.type` is dispatched (lower-cased) in `components/MessageBubble.tsx` to
trend | table | ranking | distribution | clarification.

Three things this file must therefore guarantee:
  1. `status` is never the literal string "error" on success.
  2. `error` is an OBJECT {code, message} — the UI reads `error.code`.
  3. Failures return HTTP 200 with status="error", because api.ts throws on
     `!res.ok` and the user then sees a generic message instead of the real one.

The clean /api/v1/* contract is untouched; this is a translation layer over the
same orchestrator.
"""

import hashlib
import time
import uuid
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel, Field

from app.api.deps import get_company_context
from app.api.v1.compat import infer_domain
from app.core.config import get_settings
from app.core.logging import get_logger
from app.execution.visual import build_visual

log = get_logger("legacy_api")

router = APIRouter(tags=["legacy"])

APP_VERSION = "0.1.0"




# --------------------------------------------------------------------- models


class FilterRequest(BaseModel):
    column: str
    operator: str = "eq"
    value: Any = None


class LegacyQueryRequest(BaseModel):
    """The old QueryRequest. Every field optional — the live frontend sends
    only `message`, and older builds send only `question`."""

    question: str = ""
    message: str = ""
    # The deployed frontend sends no company_id today (see frontend-patch.md
    # / DEPLOY.md). Falls back to settings.default_company_id ("beta") so
    # that existing traffic keeps working unmodified; a client can send this
    # explicitly once it's updated to support multiple companies.
    company_id: str | None = None
    source_question_id: int | str | None = None
    filters: list[FilterRequest] = Field(default_factory=list)
    limit: int = 50
    history: list[dict[str, Any]] = Field(default_factory=list)
    session_context: dict[str, Any] = Field(default_factory=dict)
    session_id: str | None = None
    thread_id: str | None = None
    client_id: str = "FRONTEND"
    use_cache: bool = True

    def text(self) -> str:
        return (self.message or self.question).strip()


# ---------------------------------------------------------------------- auth


def _require_api_key(x_api_key: str | None) -> None:
    configured = get_settings().legacy_api_key
    if configured and x_api_key != configured:
        raise HTTPException(status_code=401, detail="Invalid API key")


# ------------------------------------------------------------------ sessions


def _resolve_session_id(request: Request, body: LegacyQueryRequest) -> str:
    """Derive a stable conversation key.

    The deployed frontend sends no session identifier at all, which would make
    every request a fresh conversation and silently kill follow-up resolution
    ("sort them by margin", "what about 2024?"). Until the frontend is patched
    to send `session_id`, fall back to a hash of the caller's identity.

    Behind the Cloudflare tunnel the browser's real address arrives in
    CF-Connecting-IP; request.client.host would be the tunnel's own address and
    would collapse every user into one shared history.
    """
    explicit = body.session_id or body.thread_id
    if explicit:
        return str(explicit)[:64]

    forwarded = (
        request.headers.get("cf-connecting-ip")
        or (request.headers.get("x-forwarded-for") or "").split(",")[0].strip()
        or (request.client.host if request.client else "")
    )
    agent = request.headers.get("user-agent", "")
    digest = hashlib.sha256(f"{forwarded}|{agent}".encode()).hexdigest()[:24]
    return f"anon-{digest}"


# ------------------------------------------------------------------- visuals
# Chart-shape detection (trend/ranking/table) lives in app.execution.visual,
# shared with the streaming endpoint's `visual` event — see that module for
# the full reasoning, including why `distribution` is deliberately absent.


def _freshness(last_date: str | None) -> tuple[str | None, int | None, str | None]:
    if not last_date:
        return None, None, None
    try:
        from datetime import UTC, date, datetime

        parts = [int(p) for p in str(last_date)[:10].split("-")]
        days = (datetime.now(UTC).date() - date(*parts)).days
    except (ValueError, TypeError):
        return str(last_date), None, None
    status = "fresh" if days <= 2 else "recent" if days <= 7 else "stale"
    return str(last_date), days, status


# -------------------------------------------------------------------- routes


@router.post("/ask")
async def ask(
    request: Request,
    body: LegacyQueryRequest,
    x_eliara_key: str | None = Header(default=None),
) -> dict[str, Any]:
    """The endpoint the live website calls.

    Unauthenticated by default, because the browser calls this directly and
    holds no credential. Once the frontend proxies through its own server
    (AUTH.md), set ELIARA_ASK_SHARED_SECRET and this closes.
    """
    secret = get_settings().ask_shared_secret
    if secret and x_eliara_key != secret:
        raise HTTPException(status_code=401, detail="Unauthorized")
    return await _execute(request, body)


@router.post("/v1/query")
async def query_v1(
    request: Request,
    body: LegacyQueryRequest,
    x_api_key: str | None = Header(default=None),
) -> dict[str, Any]:
    _require_api_key(x_api_key)
    return await _execute(request, body)


async def _execute(request: Request, body: LegacyQueryRequest) -> dict[str, Any]:
    started = time.perf_counter()
    request_id = uuid.uuid4().hex[:12]
    question = body.text()

    if not question:
        return _error(request_id, "EMPTY_QUESTION", "Please enter a question.", started)

    settings = get_settings()
    company_id = body.company_id or settings.default_company_id

    try:
        ctx = get_company_context(request, company_id)
    except Exception as exc:  # noqa: BLE001 - any failure here means "unavailable",
        # regardless of exception shape; surfaced uniformly as a clean error
        # rather than letting a raw exception type leak to the client.
        public = getattr(exc, "public_message", "The analytics service is not ready. Please try again shortly.")
        code = "UNKNOWN_COMPANY" if type(exc).__name__ == "UnknownCompany" else "SERVICE_UNAVAILABLE"
        return _error(request_id, code, public, started)
    orchestrator = ctx.orchestrator

    if len(question) > settings.max_message_chars:
        return _error(
            request_id,
            "MESSAGE_TOO_LONG",
            "That question is too long. Please shorten it and try again.",
            started,
        )

    session_id = _resolve_session_id(request, body)

    limiter = getattr(request.app.state, "rate_limiter", None)
    rate_key = f"{company_id}:{session_id}"
    if limiter is not None and not limiter.allow(rate_key):
        # Deliberately a 200 + status=error: api.ts throws on !res.ok, which
        # would replace this message with a generic one.
        return _error(
            request_id,
            "RATE_LIMITED",
            "Too many requests. Please wait a moment and try again.",
            started,
        )

    try:
        outcome = await orchestrator.handle(session_id, question)
    except Exception as exc:
        log.warning("legacy_query_failed", error=type(exc).__name__, exc_info=True)
        public = getattr(
            exc, "public_message", "An internal error occurred. Please try again."
        )
        return _error(request_id, type(exc).__name__, public, started)

    metrics = getattr(request.app.state, "metrics", None)
    if metrics is not None:
        metrics["requests_total"] += 1
        if outcome.cache_hit:
            metrics["cache_hits"] += 1

    result = getattr(orchestrator, "_last_result", None)
    cfg = ctx.config
    boundaries = ctx.executor.data_boundaries(
        table=cfg.boundaries_table, date_column=cfg.boundaries_date_column
    )
    as_of, freshness_days, freshness_status = _freshness(
        boundaries.last_date if boundaries else None
    )

    domain = infer_domain(outcome.view_used, outcome.sql_generated)
    if outcome.view_used:
        endpoint_used = outcome.view_used
    elif outcome.sql_generated:
        endpoint_used = "sql:generated"
    else:
        endpoint_used = f"none:{outcome.decision}"

    total_ms = round((time.perf_counter() - started) * 1000, 1)

    return {
        "answer": outcome.answer,
        "domain": domain,
        "detail": outcome.answer,
        "endpoint_used": endpoint_used,
        "visual": build_visual(result, outcome.view_used),
        "session_context": {
            **(body.session_context or {}),
            "last_topic": domain,
            "last_view": outcome.view_used,
        },
        "timings": {
            "total_ms": total_ms,
            "stages": [
                {
                    "stage": "cache" if outcome.cache_hit else "intent routing",
                    "ms": float(outcome.routing_ms),
                },
                {"stage": "query execution", "ms": float(outcome.execution_ms)},
                {"stage": "answer narration", "ms": float(outcome.answer_ms)},
            ],
        },
        "request_id": request_id,
        "status": "success",
        "canonical_id": outcome.view_used,
        "canonical_question": None,
        "mapped_view_name": outcome.view_used,
        "rows": [list(r) for r in result.rows] if result is not None else [],
        "columns": list(result.columns) if result is not None else [],
        "row_count": result.row_count if result is not None else 0,
        "data_as_of_date": as_of,
        "freshness_days": freshness_days,
        "freshness_status": freshness_status,
        "warning_messages": [],
        "cache_hit": outcome.cache_hit,
        "elapsed_ms": total_ms,
        "error": None,
    }


def _error(request_id: str, code: str, message: str, started: float) -> dict[str, Any]:
    """HTTP 200 with status="error" — the shape ErrorBubble expects."""
    total_ms = round((time.perf_counter() - started) * 1000, 1)
    return {
        "answer": message,
        "domain": "error",
        "detail": message,
        "endpoint_used": "none:error",
        "visual": None,
        "session_context": {},
        "timings": {"total_ms": total_ms, "stages": []},
        "request_id": request_id,
        "status": "error",
        "canonical_id": None,
        "canonical_question": None,
        "mapped_view_name": None,
        "rows": [],
        "columns": [],
        "row_count": 0,
        "data_as_of_date": None,
        "freshness_days": None,
        "freshness_status": None,
        "warning_messages": [message],
        "cache_hit": False,
        "elapsed_ms": total_ms,
        "error": {"code": code, "message": message},
    }


@router.get("/health")
async def legacy_health(request: Request, company_id: str | None = None) -> dict[str, Any]:
    """Root-level /health in the old response shape, for settings.default_company_id
    (or an explicit ?company_id=) — the old frontend calls this with no
    company context, so it reports the default company's status."""
    settings = get_settings()
    manager = getattr(request.app.state, "company_manager", None)
    ctx = manager.get(company_id or settings.default_company_id) if manager is not None else None
    executor = ctx.executor if ctx is not None and ctx.healthy else None
    index = ctx.metadata_index if ctx is not None and ctx.healthy else None

    boundaries = (
        executor.data_boundaries(table=ctx.config.boundaries_table, date_column=ctx.config.boundaries_date_column)
        if executor is not None
        else None
    )
    key_present = bool(settings.anthropic_api_key.get_secret_value())

    return {
        "status": "ok" if (executor is not None and index is not None) else "degraded",
        "version": APP_VERSION,
        "active_answer_model": (
            f"Claude ({settings.orchestrator_model})"
            if key_present
            else "templates only (no LLM reachable)"
        ),
        "claude": {
            "enabled": key_present,
            "key_present": key_present,
            "model": settings.orchestrator_model,
            "sqlgen_model": settings.sqlgen_model,
        },
        "database": str(settings.db_path),
        "data_as_of": boundaries.last_date if boundaries else None,
        "v4_engines": {
            "available": index is not None,
            "database": "ready" if executor is not None else "not found",
            "engines_initialized": len(index.objects) if index is not None else 0,
        },
    }


@router.get("/stats")
async def legacy_stats(request: Request, company_id: str | None = None) -> dict[str, Any]:
    settings = get_settings()
    metrics = getattr(request.app.state, "metrics", {}) or {}
    manager = getattr(request.app.state, "company_manager", None)
    ctx = manager.get(company_id or settings.default_company_id) if manager is not None else None
    index = ctx.metadata_index if ctx is not None and ctx.healthy else None
    return {
        "status": "ok",
        "requests_total": metrics.get("requests_total", 0),
        "cache_hits": metrics.get("cache_hits", 0),
        "objects_indexed": len(index.objects) if index is not None else 0,
    }


@router.get("/recent")
async def legacy_recent() -> dict[str, Any]:
    return {"status": "ok", "recent": []}
