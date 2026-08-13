"""In-memory metadata index: search documents + SQL whitelist + candidates."""

from app.core.models import ViewCandidate
from app.discovery.models import ObjectMeta, RegistryEntry, SearchDoc

_CATEGORY_HINTS = {
    "question_view": "approved business question answer",
    "ai_view": "quick summary analytics overview",
    "gold_view": "validated gold reporting",
    "official_view": "official reporting",
    "semantic_view": "business semantic layer",
    "fact": "raw transaction fact table",
    "dim": "dimension master data",
    "engine": "materialized analytics engine",
}


class MetadataIndex:
    def __init__(
        self,
        objects: dict[str, ObjectMeta],
        registry: list[RegistryEntry],
        glossary: dict[str, str],
        fingerprint: str,
    ) -> None:
        self.objects = objects
        self.registry = registry
        self.glossary = glossary
        self.fingerprint = fingerprint

    # ---- SQL whitelist (M5 validator input) ----
    @property
    def whitelist(self) -> dict[str, set[str]]:
        return {name: set(meta.columns) for name, meta in self.objects.items()}

    # ---- retrieval corpus ----
    def searchable_documents(self) -> list[SearchDoc]:
        docs: list[SearchDoc] = []
        for meta in self.objects.values():
            words = meta.name.replace("vw_", " ").replace("_", " ")
            parts = [_CATEGORY_HINTS.get(meta.category, ""), words]
            if meta.registry:
                # canonical question is the strongest signal — weight it double
                parts = [meta.registry.canonical_question, meta.registry.canonical_question] + parts
            parts.append(" ".join(meta.columns[:40]))
            docs.append(SearchDoc(name=meta.name, text=" ".join(p for p in parts if p)))
        return docs

    def candidate(self, name: str, score: float) -> ViewCandidate:
        meta = self.objects[name]
        reg = meta.registry
        return ViewCandidate(
            view_name=name,
            kind="view" if meta.kind == "view" else "table",
            category=meta.category,
            canonical_question=reg.canonical_question if reg else None,
            columns=meta.columns,
            assumption_status=reg.assumption_status if reg else None,
            requires_endpoint_filter=reg.requires_endpoint_filter if reg else False,
            time_scope_rule=reg.time_scope_rule if reg else None,
            score=round(score, 6),
        )
