"""Entity resolution — maps what a user typed to what the database stores.

The problem this solves, observed in production:

    user:   "Can we expand more on M. M MANSUR GROUP"
    stored: "M. M MANSUR GROUP"          (customer C00594, AED 18.5M)
    filter: "M. M. Mansur Group"          (routing model's normalisation)
    SQL:    WHERE "customer_name" = ?     -> 0 rows
    answer: "No matching data was found."

SQLite's ``=`` is byte-exact, so a single extra period silently produced an
empty result set and the answer model — correctly, given what it saw — reported
that the customer does not exist. Every name-based lookup in the system had
this failure mode; only code-based lookups (C00075) worked.

Resolution runs BEFORE the query, deterministically, in Python. It costs no
tokens and cannot be prompt-injected. Four outcomes:

    exact      value already matches a stored value        -> pass through
    resolved   unique match after normalisation/fuzzing    -> substitute canonical
    ambiguous  several plausible matches                   -> ask the user which
    unknown    nothing close                               -> say so, with near misses

Real customer names in this dataset include "KARAOUI PIECE MTO S.A.R.L .",
"AL QASSEM USED CARS TR. LLCAL QASSEM USED CARS TR. LLC" and
"MRE AUTO HOLDINGS PTY LTD T/A RENNEN AUTOTEILE", so tolerant matching is not
a nicety here — exact match is unusable against human input.
"""

import re
import time
import unicodedata
from difflib import SequenceMatcher
from typing import Literal

from pydantic import BaseModel

from app.core.logging import get_logger

log = get_logger("entity_resolver")

# Legal-form noise. Stripped only for the fuzzy tier, never from stored values.
_LEGAL_SUFFIXES = {
    "llc", "l l c", "ltd", "limited", "inc", "co", "company", "corp", "plc",
    "pty", "sarl", "s a r l", "sa", "sp", "sole", "proprietorship", "est",
    "establishment", "tr", "trading", "trd", "general", "gen", "fzc", "fze",
    "dmcc", "wll", "cc", "gmbh", "bv", "nv", "ag", "srl", "spa", "kg", "ohg",
}

_PUNCT_RE = re.compile(r"[^\w\s]+", re.UNICODE)
_WS_RE = re.compile(r"\s+")
_CODE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9\-_./ ]*$")

# Guards so a large dimension table cannot blow up memory.
_MAX_ENTRIES_PER_ENTITY = 200_000
# A single entity taking longer than this at startup is a warning sign:
# the API cannot accept connections until the whole index is built.
_SLOW_ENTITY_S = 5.0
_FUZZY_THRESHOLD = 0.86
_FUZZY_CANDIDATE_CAP = 8
_AMBIGUOUS_REPORT_CAP = 5


def normalise(value: str) -> str:
    """Case-folded, punctuation-free, whitespace-collapsed."""
    text = unicodedata.normalize("NFKD", str(value))
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = _PUNCT_RE.sub(" ", text).casefold()
    return _WS_RE.sub(" ", text).strip()


def core_form(value: str) -> str:
    """``normalise`` with legal-form tokens removed, for fuzzy comparison only.

    Single-character tokens are dropped too: "S.A.R.L ." and "L.L.C" shatter
    into "s a r l" and "l l c" under normalisation, which no whole-word suffix
    list can catch. So "KARAOUI PIECE MTO S.A.R.L ." and "Karaoui Piece MTO
    SARL" both reduce to "karaoui piece mto".
    """
    tokens = [
        t
        for t in normalise(value).split()
        if t not in _LEGAL_SUFFIXES and len(t) > 1
    ]
    return " ".join(tokens) or normalise(value)


def looks_like_code(value: str) -> bool:
    """Codes here take many shapes: "C00075", "1ZS 011 939-411",
    "T 221 880 1A 40-KIT". What they share is digits, few tokens, and no
    sentence punctuation — unlike "SKODA SUPERB (2016-2019). HEADLAMP ...".
    """
    text = str(value).strip()
    if not text or not _CODE_RE.match(text):
        return False
    if sum(c.isdigit() for c in text) < 2:
        return False
    return len(text.split()) <= 4


class Resolution(BaseModel):
    status: Literal["exact", "resolved", "ambiguous", "unknown", "no_index"]
    column: str
    requested: str
    value: str | None = None          # canonical value to filter on
    candidates: list[str] = []        # human-readable, for clarify / near misses

    @property
    def usable(self) -> bool:
        return self.status in {"exact", "resolved", "no_index"}


class _EntityTable(BaseModel):
    """One entity's lookup structures (e.g. "customer")."""

    entity: str
    source: str
    code_by_norm_name: dict[str, list[str]] = {}
    name_by_norm_name: dict[str, list[str]] = {}
    name_by_code: dict[str, str] = {}
    core_index: dict[str, list[str]] = {}   # core form -> canonical names
    exact_names: set[str] = set()
    exact_codes: set[str] = set()


class EntityIndex:
    """Built once at startup and rebuilt on database refresh."""

    def __init__(self, tables: dict[str, _EntityTable]) -> None:
        self._tables = tables

    @property
    def entities(self) -> list[str]:
        return sorted(self._tables)

    def __len__(self) -> int:
        return sum(len(t.exact_names) for t in self._tables.values())

    # ------------------------------------------------------------- resolution
    def resolve(self, column: str, value: str) -> Resolution:
        entity, kind = _split_column(column)
        table = self._tables.get(entity) if entity else None
        if table is None or not value:
            # No index for this column — never block the query on our account.
            return Resolution(status="no_index", column=column, requested=value, value=value)

        raw = str(value).strip()

        # 1. Already exactly what the database stores.
        if (kind == "name" and raw in table.exact_names) or (
            kind == "code" and raw in table.exact_codes
        ):
            return Resolution(status="exact", column=column, requested=raw, value=raw)

        norm = normalise(raw)

        # 2. The user gave a code but the filter column is a name (or vice
        #    versa). Very common: "expand on customer C00075".
        if kind == "name" and looks_like_code(raw):
            canonical = table.name_by_code.get(norm)
            if canonical:
                return Resolution(
                    status="resolved", column=column, requested=raw, value=canonical
                )
        if kind == "code":
            names = table.code_by_norm_name.get(norm)
            if names and len(set(names)) == 1:
                return Resolution(
                    status="resolved", column=column, requested=raw, value=names[0]
                )
            if names:
                return Resolution(
                    status="ambiguous", column=column, requested=raw,
                    candidates=sorted(set(names))[:_AMBIGUOUS_REPORT_CAP],
                )
            return Resolution(status="unknown", column=column, requested=raw)

        # 3. Case / punctuation only. This is the Mansur case.
        matches = table.name_by_norm_name.get(norm)
        if matches:
            unique = sorted(set(matches))
            if len(unique) == 1:
                return Resolution(
                    status="resolved", column=column, requested=raw, value=unique[0]
                )
            return Resolution(
                status="ambiguous", column=column, requested=raw,
                candidates=unique[:_AMBIGUOUS_REPORT_CAP],
            )

        # 4. Legal-suffix-insensitive exact match.
        core = core_form(raw)
        matches = table.core_index.get(core)
        if matches:
            unique = sorted(set(matches))
            if len(unique) == 1:
                return Resolution(
                    status="resolved", column=column, requested=raw, value=unique[0]
                )
            return Resolution(
                status="ambiguous", column=column, requested=raw,
                candidates=unique[:_AMBIGUOUS_REPORT_CAP],
            )

        # 5. Fuzzy, restricted to entries sharing a rare-ish token so we never
        #    scan the whole dimension.
        near = self._fuzzy(table, raw, core)
        if len(near) == 1:
            return Resolution(status="resolved", column=column, requested=raw, value=near[0])
        if near:
            return Resolution(
                status="ambiguous", column=column, requested=raw,
                candidates=near[:_AMBIGUOUS_REPORT_CAP],
            )
        return Resolution(status="unknown", column=column, requested=raw)

    def _fuzzy(self, table: _EntityTable, raw: str, core: str) -> list[str]:
        tokens = [t for t in core.split() if len(t) > 2]
        if not tokens:
            return []
        pool: set[str] = set()
        for key, names in table.core_index.items():
            if any(t in key for t in tokens):
                pool.update(names)
                if len(pool) > 5_000:  # pathological prefix; bail to avoid O(n)
                    break
        scored: list[tuple[float, str]] = []
        for name in pool:
            ratio = SequenceMatcher(None, core, core_form(name)).ratio()
            if ratio >= _FUZZY_THRESHOLD:
                scored.append((ratio, name))
        scored.sort(reverse=True)
        # A clear winner beats a pack of near-ties.
        if len(scored) > 1 and scored[0][0] - scored[1][0] > 0.08:
            return [scored[0][1]]
        return [n for _, n in scored[:_FUZZY_CANDIDATE_CAP]]


def _split_column(column: str) -> tuple[str | None, str]:
    """"customer_name" -> ("customer", "name")."""
    lowered = column.lower()
    for suffix in ("_name", "_code"):
        if lowered.endswith(suffix):
            return lowered[: -len(suffix)], suffix[1:]
    return None, ""


def build_entity_index(
    executor,
    objects: dict,
    include_facts: bool = False,
    overrides: str = "",
) -> EntityIndex:
    """Scan tables for ``{entity}_code`` / ``{entity}_name`` pairs and load them.

    Dimension tables only, by default. A fact table can supply an entity that
    has no dimension (sales_employee, typically), but ``SELECT DISTINCT`` over
    a multi-million-row unindexed fact table is a full scan — and this runs
    inside application startup, before uvicorn binds its port. A slow scan here
    does not look like a slow scan; it looks like "connection refused".

    Set ``include_facts=True`` (ELIARA_ENTITY_INDEX_INCLUDE_FACTS) only if you
    need those entities and have measured the cost on your real database.

    ``overrides`` (ELIARA_ENTITY_INDEX_SOURCES) names a source explicitly when
    auto-detection misses one, as comma-separated ``entity=object`` pairs:

        customer=fact_ai_sales_net,supplier=vw_b3_supplier_master

    An override wins over detection and bypasses the category filter entirely,
    so it can point at a fact table or a view without opening the door for
    every other entity.
    """
    allowed = {"dim"} | ({"fact"} if include_facts else set())

    sources: dict[str, tuple[str, str, str]] = {}  # entity -> (source, code, name)

    # Explicit overrides first — they must not be displaced by detection.
    for pair in (o.strip() for o in overrides.split(",") if o.strip()):
        entity, _, object_name = pair.partition("=")
        entity, object_name = entity.strip(), object_name.strip()
        meta = objects.get(object_name)
        if not entity or meta is None:
            log.warning(
                "entity_override_unknown", entity=entity, source=object_name,
                hint="object not present in this database",
            )
            continue
        code_col, name_col = f"{entity}_code", f"{entity}_name"
        if name_col not in meta.columns:
            log.warning(
                "entity_override_missing_column", entity=entity,
                source=object_name, expected=name_col, columns=meta.columns[:12],
            )
            continue
        sources[entity] = (
            object_name,
            code_col if code_col in meta.columns else name_col,
            name_col,
        )
        log.info("entity_override_applied", entity=entity, source=object_name)

    # Dimensions win over facts; tables win over views.
    ranked = sorted(
        objects.items(),
        key=lambda kv: (
            kv[1].category != "dim",
            kv[1].kind != "table",
            kv[1].category != "fact",
            kv[0],
        ),
    )
    for name, meta in ranked:
        if meta.category not in allowed:
            continue
        if meta.kind not in ("table", "view"):
            continue
        columns = set(meta.columns)
        for column in meta.columns:
            if not column.lower().endswith("_code"):
                continue
            entity = column[: -len("_code")]
            partner = f"{entity}_name"
            if partner in columns and entity not in sources:
                sources[entity] = (name, column, partner)

    tables: dict[str, _EntityTable] = {}
    started_all = time.perf_counter()

    for entity, (source, code_col, name_col) in sources.items():
        started = time.perf_counter()
        try:
            result = executor.run_metadata_sql(
                f'SELECT DISTINCT "{code_col}", "{name_col}" FROM "{source}" '
                f'WHERE "{name_col}" IS NOT NULL AND TRIM("{name_col}") != ""',
                row_cap=_MAX_ENTRIES_PER_ENTITY,
            )
        except Exception as exc:  # noqa: BLE001 - a missing/odd table must not break startup
            log.warning("entity_source_failed", entity=entity, source=source,
                        error=type(exc).__name__)
            continue

        table = _EntityTable(entity=entity, source=source)
        for code, name in result.rows:
            name = str(name).strip()
            if not name:
                continue
            norm = normalise(name)
            table.exact_names.add(name)
            table.name_by_norm_name.setdefault(norm, []).append(name)
            table.core_index.setdefault(core_form(name), []).append(name)
            if code is not None and str(code).strip():
                code = str(code).strip()
                table.exact_codes.add(code)
                table.name_by_code.setdefault(normalise(code), name)
                table.code_by_norm_name.setdefault(norm, []).append(code)

        elapsed = time.perf_counter() - started
        if table.exact_names:
            tables[entity] = table
        log.info(
            "entity_source_loaded",
            entity=entity,
            source=source,
            values=len(table.exact_names),
            elapsed_ms=int(elapsed * 1000),
        )
        if elapsed > _SLOW_ENTITY_S:
            log.warning(
                "entity_source_slow",
                entity=entity,
                source=source,
                elapsed_s=round(elapsed, 1),
                hint="startup blocks here; consider excluding this source",
            )

    index = EntityIndex(tables)
    log.info(
        "entity_index_built",
        entities=index.entities,
        total_values=len(index),
        elapsed_ms=int((time.perf_counter() - started_all) * 1000),
        sources={e: t.source for e, t in tables.items()},
    )
    return index
