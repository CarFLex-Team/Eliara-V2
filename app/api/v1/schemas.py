"""API request/response contracts (v1)."""

from typing import Literal

from pydantic import BaseModel, Field


class HealthResponse(BaseModel):
    status: str = "ok"
    version: str


class DeepHealthResponse(BaseModel):
    status: str
    database: str
    metadata_index: str
    llm: str
    requests_total: int = 0
    cache_hits: int = 0
    last_db_refresh: str | None = None
    # "full" or "partial" — a completeness signal distinct from `status`
    # (which reports runtime health). A partial company boots and answers
    # questions normally; it's just missing curated playbooks/scan views/
    # glossary data compared to a full company. See CompanyConfig.status.
    config_status: str = "full"


class ChatRequest(BaseModel):
    company_id: str = Field(min_length=1, max_length=64, pattern=r"^[a-z0-9_]+$")
    session_id: str = Field(min_length=1, max_length=64)
    message: str = Field(min_length=1)


class ResponseMeta(BaseModel):
    view_used: str | None = None
    sql_generated: bool = False
    cache_hit: bool = False
    latency_ms: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    # "external" for a /search answer (no governed data, may include live web
    # results), "data" for anything answered from the company's own database.
    # Lets the frontend badge external answers without re-detecting the
    # prefix client-side — the server's classification is the source of truth.
    source: Literal["data", "external"] = "data"


class ChatResponse(BaseModel):
    answer: str
    meta: ResponseMeta
