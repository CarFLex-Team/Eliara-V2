"""Liveness and deep health endpoints.

Deep health is company-aware: a failure in one company's database/metadata
must never make another company appear healthy, and must never be hidden
by an aggregate "ok". ``GET /health/deep`` with no ``company_id`` returns
every registered company's status in one response so an operator can see
both at a glance; ``?company_id=`` narrows to one company's detail in the
same shape as before this refactor.
"""

import datetime

from fastapi import APIRouter, Request

from app.api.v1.schemas import DeepHealthResponse, HealthResponse

APP_VERSION = "0.1.0"

router = APIRouter(tags=["health"])


@router.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    return HealthResponse(version=APP_VERSION)


def _company_health(ctx) -> DeepHealthResponse:
    from app.core.config import get_settings

    if not ctx.healthy:
        return DeepHealthResponse(
            status="degraded",
            database=f"unavailable: {ctx.startup_error}",
            metadata_index="not_built",
            llm="unknown",
        )

    boundaries = None
    cfg = ctx.config
    if cfg.boundaries_table and cfg.boundaries_date_column:
        boundaries = ctx.executor.data_boundaries(
            table=cfg.boundaries_table, date_column=cfg.boundaries_date_column
        )
    database = f"ok (data through {boundaries.last_date})" if boundaries else "ok"

    index, retriever = ctx.metadata_index, ctx.retriever
    metadata_index = (
        f"ok ({len(index.objects)} objects, {retriever.mode})"
        if index is not None and retriever is not None
        else "not_built"
    )

    llm = (
        "key_configured"
        if get_settings().anthropic_api_key.get_secret_value()
        else "no_api_key"
    )

    last_refresh = None
    if ctx.watcher is not None and ctx.watcher.last_change_detected:
        last_refresh = datetime.datetime.fromtimestamp(
            ctx.watcher.last_change_detected, tz=datetime.UTC
        ).isoformat()

    healthy = database.startswith("ok") and metadata_index.startswith("ok")
    return DeepHealthResponse(
        status="ok" if healthy else "degraded",
        database=database,
        metadata_index=metadata_index,
        llm=llm,
        last_db_refresh=last_refresh,
    )


@router.get("/health/deep")
async def deep_health(request: Request, company_id: str | None = None):
    manager = getattr(request.app.state, "company_manager", None)
    metrics = getattr(request.app.state, "metrics", {}) or {}

    if manager is None:
        return DeepHealthResponse(
            status="degraded", database="not_found", metadata_index="not_built", llm="unknown"
        )

    if company_id is not None:
        ctx = manager.get(company_id)  # raises UnknownCompany -> 404 via error handler
        resp = _company_health(ctx)
        resp.requests_total = metrics.get("requests_total", 0)
        resp.cache_hits = metrics.get("cache_hits", 0)
        return resp

    # No company_id: report every registered company independently, plus a
    # platform-level requests_total/cache_hits that isn't scoped to any one
    # company. A degraded company never flips another company's entry.
    companies = {
        cid: _company_health(ctx).model_dump()
        for cid, ctx in manager.all_contexts().items()
    }
    overall = "ok" if all(c["status"] == "ok" for c in companies.values()) else "degraded"
    return {
        "status": overall,
        "requests_total": metrics.get("requests_total", 0),
        "cache_hits": metrics.get("cache_hits", 0),
        "companies": companies,
    }
