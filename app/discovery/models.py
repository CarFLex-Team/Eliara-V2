"""Discovery-layer data structures."""

from pydantic import BaseModel


class RegistryEntry(BaseModel):
    question_id: int
    canonical_question: str
    view_name: str
    assumption_status: str | None = None
    time_scope_rule: str | None = None
    requires_endpoint_filter: bool = False
    enabled: bool = True


class ObjectMeta(BaseModel):
    name: str
    kind: str            # "table" | "view"
    category: str        # question_view | ai_view | gold_view | official_view |
                         # semantic_view | fact | dim | engine
    columns: list[str]
    # One real value per column, as a display string. Column NAMES alone force
    # the SQL generator to GUESS value formats — `year_month` could hold
    # "202604" or "2026-04", and a wrong guess returns zero rows that read as
    # "no data exists" rather than "the filter never matched". Captured once
    # at discovery, not per request.
    samples: dict[str, str] = {}
    registry: RegistryEntry | None = None


class SearchDoc(BaseModel):
    name: str
    text: str
