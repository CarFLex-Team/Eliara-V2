"""Shared request-time helpers for resolving a company context.

Every route that needs company-scoped resources (executor, orchestrator,
metadata index, etc.) goes through ``get_company_context`` instead of
reaching into ``request.app.state`` directly — this is the one place that
decides what happens when the registry failed to load, the platform isn't
ready, or the company_id doesn't exist.
"""

from fastapi import Request

from app.company.context import CompanyContext
from app.company.registry import UnknownCompany
from app.core.errors import EliaraError


class ServiceUnavailable(EliaraError):
    status_code = 503
    public_message = "The analytics service is not ready. Please try again shortly."


class CompanyUnavailable(EliaraError):
    status_code = 503
    public_message = "This company's analytics service is temporarily unavailable."


def get_company_context(request: Request, company_id: str) -> CompanyContext:
    manager = getattr(request.app.state, "company_manager", None)
    if manager is None:
        raise ServiceUnavailable(internal_detail="company_manager not initialized")
    ctx = manager.get(company_id)  # raises UnknownCompany (404) if not registered
    if not ctx.healthy:
        raise CompanyUnavailable(
            internal_detail=f"company {company_id!r} context unhealthy: {ctx.startup_error}"
        )
    return ctx
