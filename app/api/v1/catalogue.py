"""Business glossary and metric catalogue.

Both already exist in the database and are loaded at startup — the glossary
into ``MetadataIndex.glossary``, the metric definitions into
``chatbot_question_view_registry`` — but neither was ever reachable by a user.
So "dead stock", "churn risk" and "net revenue" meant whatever the reader
assumed they meant, and the governance work was invisible.

    GET /api/v1/glossary          business term definitions
    GET /api/v1/metrics           the metric catalogue, with lineage
    GET /api/v1/metrics/{view}    one metric in full

The metric catalogue is the honest answer to "where does this number come
from?" — it exposes the formula version and the validation status of every
curated calculation the system can run.
"""

from typing import Any

from fastapi import APIRouter, HTTPException, Request

from app.api.deps import get_company_context

router = APIRouter(tags=["catalogue"])


@router.get("/glossary")
async def glossary(request: Request, company_id: str, q: str | None = None) -> dict[str, Any]:
    """Business term definitions for one company, optionally filtered by substring."""
    ctx = get_company_context(request, company_id)
    terms = ctx.metadata_index.glossary or {}
    if q:
        needle = q.lower()
        terms = {
            term: definition
            for term, definition in terms.items()
            if needle in term.lower() or needle in (definition or "").lower()
        }
    return {
        "count": len(terms),
        "terms": [
            {"term": term, "definition": definition}
            for term, definition in sorted(terms.items())
        ],
    }


@router.get("/playbooks")
async def playbooks(request: Request, company_id: str) -> dict[str, Any]:
    """The multi-step reviews available for one company, and whether that
    company's database supports them.

    ``steps_runnable`` is lower than ``steps_total`` when a playbook references
    a view that is not present here — five of the catalogue's 100 questions are
    blocked on unloaded SAP sources, and playbooks touching those still run
    with the sections that do work.
    """
    ctx = get_company_context(request, company_id)
    library = getattr(ctx.orchestrator, "_playbooks", None)
    if library is None:
        return {"count": 0, "playbooks": []}
    catalogue = library.catalogue(set(ctx.metadata_index.objects))
    return {
        "count": len(catalogue),
        "runnable": sum(1 for p in catalogue if p["runnable"]),
        "playbooks": catalogue,
    }


@router.get("/metrics")
async def metrics(request: Request, company_id: str, status: str | None = None) -> dict[str, Any]:
    """The metric catalogue for one company: every curated question, with its lineage.

    ``status`` filters on validation state, e.g. ``APPROVED_LOGIC``, so a
    reviewer can list exactly which calculations are still provisional.
    """
    ctx = get_company_context(request, company_id)
    entries = [entry for entry in ctx.metadata_index.registry if entry.enabled]
    if status:
        entries = [
            e for e in entries
            if (e.assumption_status or "").upper() == status.upper()
        ]

    catalogue = [
        {
            "question_id": entry.question_id,
            "question": entry.canonical_question,
            "metric": entry.view_name,
            "formula_version": entry.formula_version,
            "assumption_status": entry.assumption_status,
            "validated": (entry.assumption_status or "") == "APPROVED_LOGIC",
            "time_scope_rule": entry.time_scope_rule,
            "requires_entity": entry.requires_endpoint_filter,
        }
        for entry in entries
    ]
    validated = sum(1 for m in catalogue if m["validated"])
    return {
        "count": len(catalogue),
        "validated": validated,
        "provisional": len(catalogue) - validated,
        "metrics": sorted(catalogue, key=lambda m: m["question_id"]),
    }


@router.get("/metrics/{view_name}")
async def metric_detail(request: Request, view_name: str, company_id: str) -> dict[str, Any]:
    """One metric in full, including the columns it returns."""
    ctx = get_company_context(request, company_id)
    entry = next((e for e in ctx.metadata_index.registry if e.view_name == view_name), None)
    if entry is None:
        raise HTTPException(status_code=404, detail="Unknown metric")
    meta = ctx.metadata_index.objects.get(view_name)
    return {
        "question_id": entry.question_id,
        "question": entry.canonical_question,
        "metric": entry.view_name,
        "formula_version": entry.formula_version,
        "assumption_status": entry.assumption_status,
        "validated": (entry.assumption_status or "") == "APPROVED_LOGIC",
        "time_scope_rule": entry.time_scope_rule,
        "requires_entity": entry.requires_endpoint_filter,
        "columns": meta.columns if meta else [],
    }
