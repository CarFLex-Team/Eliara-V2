"""Loads discovery metadata FROM the database itself.

The Eliara database ships its own governance metadata (the chatbot question
registry, glossary, catalogs). We read it at startup instead of re-creating
business knowledge in application code — the platform's "never recreate
existing logic" rule applied to metadata.
"""

import hashlib
import re

from app.core.errors import SQLExecutionError
from app.core.logging import get_logger
from app.discovery.models import ObjectMeta, RegistryEntry

log = get_logger("metadata_loader")

_MAX_SAMPLE_CHARS = 40
_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_ELIGIBLE_TABLE_PREFIXES = ("fact_", "dim_", "engine_")


def _category(name: str, kind: str) -> str | None:
    if kind == "view":
        if "batch" in name:
            return None  # governance evidence views — metadata, not analytics
        if name.startswith("vw_q"):
            return "question_view"
        if name.startswith("vw_ai_"):
            return "ai_view"
        if name.startswith("vw_gold_"):
            return "gold_view"
        if name.startswith("vw_official"):
            return "official_view"
        if name.startswith("vw_"):
            return "semantic_view"
        return None
    if name.startswith("fact_"):
        return "fact"
    if name.startswith("dim_"):
        return "dim"
    if name.startswith("engine_"):
        return "engine"
    return None  # SAP reference layer, batch evidence, staging: never exposed


class MetadataLoader:
    def __init__(self, executor) -> None:
        self._executor = executor

    def load(self) -> tuple[dict[str, ObjectMeta], list[RegistryEntry], dict[str, str], str]:
        master = self._executor.run_metadata_sql(
            "SELECT name, type, sql FROM sqlite_master "
            "WHERE type IN ('table','view') AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )
        registry = self._load_registry()
        registry_by_view = {e.view_name: e for e in registry}

        objects: dict[str, ObjectMeta] = {}
        fingerprint_parts: list[str] = []
        for name, kind, ddl in master.rows:
            category = _category(name, kind)
            if category is None or not _NAME_RE.match(name):
                continue
            try:
                columns = self._columns_of(name)
            except Exception as exc:  # noqa: BLE001 - one broken object must not crash startup
                # pragma_table_info resolves a VIEW's column list by
                # validating its underlying SELECT — a view left referencing
                # a column a base table no longer has (a rename, a dropped
                # column) fails here, not later. Before this fix that
                # exception was uncaught: the entire platform failed to
                # start over one stale view, instead of everything else
                # loading normally with this one object excluded.
                log.warning("object_metadata_unavailable", object=name, reason=str(exc)[:200])
                continue
            objects[name] = ObjectMeta(
                name=name,
                kind=kind,
                category=category,
                columns=columns,
                samples=self._samples_of(name),
                registry=registry_by_view.get(name),
            )
            fingerprint_parts.append(f"{name}\x00{ddl or ''}")

        fingerprint = hashlib.sha256("\x01".join(sorted(fingerprint_parts)).encode()).hexdigest()[:16]
        glossary = self._load_glossary()
        log.info(
            "metadata_loaded",
            objects=len(objects),
            registry_entries=len(registry),
            glossary_terms=len(glossary),
            fingerprint=fingerprint,
        )
        return objects, registry, glossary, fingerprint

    def _columns_of(self, name: str) -> list[str]:
        result = self._executor.run_metadata_sql(f"SELECT name FROM pragma_table_info('{name}')")
        return [r[0] for r in result.rows]

    def _samples_of(self, name: str) -> dict[str, str]:
        """One real row per object, as display strings — the cheapest possible
        cure for value-format guessing in generated SQL.

        Best-effort by design: an empty object, a view that errors, or a
        permissions issue must not break discovery, so any failure yields no
        samples rather than propagating. Values are truncated because the
        point is the SHAPE ("2026-04"), not the content.
        """
        try:
            result = self._executor.run_metadata_sql(f"SELECT * FROM {name} LIMIT 1")
        except Exception as exc:  # noqa: BLE001 - samples are a nicety, never load-bearing
            log.warning("sample_row_unavailable", object=name, reason=str(exc)[:200])
            return {}
        if not result.rows:
            return {}
        row = result.rows[0]
        samples: dict[str, str] = {}
        for column, value in zip(result.columns, row, strict=False):
            if value is None:
                continue
            text = str(value).strip()
            if text:
                samples[column] = text[:_MAX_SAMPLE_CHARS]
        return samples

    def _load_registry(self) -> list[RegistryEntry]:
        try:
            result = self._executor.run_metadata_sql(
                "SELECT question_id, canonical_question, view_name, assumption_status,"
                " time_scope_rule, requires_endpoint_filter, enabled"
                " FROM chatbot_question_view_registry ORDER BY question_id"
            )
        except SQLExecutionError:
            log.warning("registry_missing")
            return []
        return [
            RegistryEntry(
                question_id=r[0],
                canonical_question=r[1],
                view_name=r[2],
                assumption_status=r[3],
                time_scope_rule=r[4],
                requires_endpoint_filter=bool(r[5]),
                enabled=bool(r[6]),
            )
            for r in result.rows
        ]

    def _load_glossary(self) -> dict[str, str]:
        for source in ("vw_gold_business_glossary", "batch_13_business_glossary"):
            try:
                result = self._executor.run_metadata_sql(f"SELECT * FROM \"{source}\"", row_cap=2000)
            except SQLExecutionError:
                continue
            if len(result.columns) >= 2 and result.rows:
                return {str(r[0]): str(r[1]) for r in result.rows if r[0]}
        return {}
