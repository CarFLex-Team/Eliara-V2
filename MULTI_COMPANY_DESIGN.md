# Eliara — Multi-Company Design (Milestone 1)

Proposed target architecture, built by extending existing components
(§12 of CURRENT_ARCHITECTURE.md) rather than replacing them.

## 1. Company Registry & Configuration

New, small module: `app/company/registry.py`.

```python
class CompanyConfig(BaseModel):
    company_id: str                 # "beta" | "tire_guru" — validated slug
    display_name: str
    db_path: Path
    scan_views: list[str] = []
    prompts_dir: Path | None = None     # company-specific prompt overrides
    playbooks_dir: Path | None = None   # company-specific playbook YAMLs
    embedding_cache_dir: Path | None = None
    audit_subdir: str | None = None     # defaults to company_id
    # Room for future per-company knobs (answer_char_budget overrides, etc.)
    # without touching global Settings.

class CompanyRegistry:
    @classmethod
    def from_file(cls, path: Path) -> "CompanyRegistry": ...
    def get(self, company_id: str) -> CompanyConfig: ...   # raises UnknownCompany
    def all_ids(self) -> list[str]: ...
```

Loaded from a small config file/env var referenced by the brief:

```
ELIARA_COMPANIES_CONFIG=companies.yaml
```

```yaml
companies:
  beta:
    display_name: "Beta"
    db_path: data/companies/beta/beta.db
    scan_views: [...]                 # the 7 views currently in Settings
    playbooks_dir: companies/beta/playbooks
  tire_guru:
    display_name: "Tire Guru"
    db_path: data/companies/tire_guru/tire_guru.db
    scan_views: []                    # populated once Tire Guru's views are confirmed
    playbooks_dir: companies/tire_guru/playbooks
```

This directly satisfies the brief's "Do not hardcode production filesystem
paths / database filenames" requirement and keeps `Settings` for genuinely
platform-wide knobs only (`log_level`, `chat_rate_limit_per_min`,
`llm_timeout_s`, `request_deadline_s`, `legacy_api_enabled`, etc.).

`Settings.scan_views` and the *default* `Settings.db_path` are removed/
deprecated in favor of `CompanyConfig`, one per company, resolved through
the registry — closing finding #4 (and #2) from the architecture doc.

## 2. `CompanyContext` and `CompanyContextManager`

New module: `app/company/context.py`. This is a thin composition layer that
reuses `build_discovery`, `build_entity_index`, `ReadOnlyExecutor`,
`DatabaseWatcher`, `PromptManager`, `PlaybookLibrary`, `ResultCache`,
`InMemoryConversationStore`, and `Orchestrator` exactly as they exist today
— it does not reimplement any of them.

```python
@dataclass
class CompanyContext:
    company_id: str
    config: CompanyConfig
    executor: ReadOnlyExecutor
    metadata_index: MetadataIndex
    retriever: HybridRetriever
    entities: EntityIndex
    prompts: PromptManager          # shared-prompt loader + company overrides
    conversations: InMemoryConversationStore
    result_cache: ResultCache
    audit: AuditTrail
    orchestrator: Orchestrator
    watcher: DatabaseWatcher

class CompanyContextManager:
    def __init__(self, registry: CompanyRegistry, settings: Settings, llm: AnthropicClient):
        self._registry = registry
        self._settings = settings
        self._llm = llm
        self._contexts: dict[str, CompanyContext] = {}
        self._lock = threading.Lock()   # guards lazy creation only

    def get(self, company_id: str) -> CompanyContext:
        """Lazily build-and-cache a company's full runtime context.
        Thread-safe double-checked creation; steady-state reads are lock-free
        dict lookups (existing components are already internally thread-safe:
        ReadOnlyExecutor's pool is a queue.Queue, ResultCache/RateLimiter use
        their own locks)."""
        ctx = self._contexts.get(company_id)
        if ctx is not None:
            return ctx
        with self._lock:
            ctx = self._contexts.get(company_id)
            if ctx is None:
                ctx = self._build(company_id)
                self._contexts[company_id] = ctx
        return ctx

    def _build(self, company_id: str) -> CompanyContext:
        cfg = self._registry.get(company_id)          # raises UnknownCompany -> 404
        executor = ReadOnlyExecutor(cfg.db_path, ...)
        index, retriever = build_discovery(executor, self._settings)
        entities = build_entity_index(executor, index.objects, ...)
        prompts = PromptManager.for_company(cfg)        # see §4
        playbooks = PlaybookLibrary.load(cfg.playbooks_dir)  # see §5
        cache = ResultCache(ttl_s=self._settings.result_cache_ttl_s)
        conversations = InMemoryConversationStore(...)
        audit = AuditTrail(self._settings.audit_dir / (cfg.audit_subdir or company_id))
        orchestrator = Orchestrator(retriever, index, executor, prompts,
                                     conversations, self._llm, self._settings,
                                     result_cache=cache, audit=audit,
                                     playbooks=playbooks, scan_views=cfg.scan_views)
        watcher = DatabaseWatcher(cfg.db_path, interval_s=self._settings.db_watch_interval_s)
        watcher.on_change(lambda: self._refresh(company_id))
        watcher.start()
        return CompanyContext(...)

    def _refresh(self, company_id: str) -> None:
        """Atomic swap, scoped to ONE company — same pattern main.py already
        uses today, just narrowed to ctx instead of app.state."""
        ctx = self._contexts[company_id]
        ctx.executor.reopen()
        ctx.result_cache.clear()
        new_index, new_retriever = build_discovery(ctx.executor, self._settings)
        new_entities = build_entity_index(ctx.executor, new_index.objects, ...)
        ctx.metadata_index, ctx.retriever, ctx.entities = new_index, new_retriever, new_entities
        ctx.orchestrator._index = new_index
        ctx.orchestrator._retriever = new_retriever
        ctx.orchestrator._entities = new_entities
```

This satisfies the brief's requested access pattern almost verbatim
(`ctx = company_manager.get(company_id)` → `ctx.executor`, `.metadata_index`,
`.retriever`, `.entities`, `.prompts`, `.config`) while reusing every
existing building block unchanged. It is the direct generalization of what
`main.py`'s `lifespan()` + `_on_db_refresh()` closure already does today —
narrowed from "the one app" to "one company," called N times.

`app.state.company_manager` replaces the current `app.state.orchestrator`
etc. `app.state.rate_limiter` and `app.state.metrics` can stay
process-global (rate limiting and total request metrics are reasonably
platform-level), but the rate-limiter key changes from `session_id` to
`f"{company_id}:{session_id}"` (closes finding #7) and metrics gain a
per-company breakdown if desired (nice-to-have, not required by the
acceptance criteria).

## 3. Session & cache key isolation

- `InMemoryConversationStore`: change the key type from `session_id: str`
  to a composite key. Minimal-diff option: keep the class as-is and give
  **each company its own `InMemoryConversationStore` instance** (one per
  `CompanyContext`, as sketched above) — this closes finding #5 with zero
  changes to `conversation.py` itself, at the cost of N conversation stores
  instead of one. This matches the brief's "adapted instead of replaced."
- `ResultCache`: same reasoning — one `ResultCache` instance per company
  context closes finding #6 with zero changes to `cache.py`. (Alternative:
  keep one shared cache and prefix every key tuple with `company_id`; the
  per-context instance is simpler and matches the pool-isolation pattern
  used for the executor, so it's the recommended default.)
- `AuditTrail`: one instance per company, writing under
  `audit_dir/<company_id>/audit-YYYY-MM-DD.jsonl`, OR keep one shared
  instance and add `company_id` as a required field on `record(...)`. Given
  the brief explicitly shows `company_id` inside the JSON audit event
  example, the recommended approach is: **keep one shared `AuditTrail`,
  add `company_id` as a required parameter to `record()`** — this is a
  smaller diff than N audit directories and keeps a single
  chronologically-sorted operational log, which is more useful for
  cross-company on-call debugging than per-company audit directories.
  (Per-company subdirectory remains an easy option if the user prefers full
  filesystem separation — flagged as an open question for Milestone 6.)

## 4. Prompts

Keep `PromptManager` exactly as-is (it already does versioned YAML +
Jinja2 rendering with no hardcoded company data). Add one small factory:

```python
class PromptManager:
    @classmethod
    def for_company(cls, cfg: CompanyConfig) -> "PromptManager":
        """Loads app/prompts/shared/templates/**  (always)
        then, if cfg.prompts_dir is set, ALSO loads that directory,
        with company templates taking precedence on name collision."""
```

Directory layout (per brief's recommended structure):

```
app/prompts/shared/templates/{orchestrator,agent,sqlgen,external}/*.yaml   # existing files, moved as-is
companies/beta/prompts/...          # only if/when a Beta-specific prompt is needed
companies/tire_guru/prompts/...     # only if/when a Tire Guru-specific prompt is needed
```

Since the current inventory of prompt YAMLs contains **no hardcoded
company name or view name** (finding in CURRENT_ARCHITECTURE.md §7), no
prompt content actually needs to move or fork on day one — this satisfies
"do not duplicate prompts unnecessarily." Company folders are created
empty/ready, and a prompt only moves into a company folder the day it
needs company-specific wording (e.g. a Tire Guru-specific glossary
explanation, if that ever becomes necessary — glossary *values* already
flow in as data via `self._index.glossary`, not as prompt text).

## 5. Playbooks

`PlaybookLibrary.load()` gains an optional directory argument:

```python
class PlaybookLibrary:
    @classmethod
    def load(cls, extra_dir: Path | None = None) -> "PlaybookLibrary":
        # existing behavior: always load app/orchestrator/definitions/*.yaml
        # (kept as the SHARED/example set, or emptied to company dirs — see below)
        # if extra_dir: also load extra_dir/*.yaml
```

The five existing playbook YAMLs (`investigate_customer`,
`procurement_plan`, `supplier_review`, `stock_action_plan`,
`business_review`) all name real Beta views — per the brief these move to
`companies/beta/playbooks/`, loaded via `cfg.playbooks_dir`. Tire Guru
starts with an empty `companies/tire_guru/playbooks/` directory; playbooks
are added there once Tire Guru's actual views are known (out of scope for
this milestone — no Tire Guru schema has been provided yet, see
MIGRATION_PLAN.md open questions). The core playbook *engine*
(`playbooks.py`'s execution/aggregation logic) is untouched.

`scan_views` moves from global `Settings` into `CompanyConfig.scan_views`
(§1) and is passed into `Orchestrator` per company instead of read from
`self._settings.scan_views`.

## 6. SQL generation

`SQLGenerator` (`app/sqlgen/generator.py`) is already built per-Orchestrator
-instance from injected `llm`/`prompts`/`settings` and receives schema
context per-call from `self._index`/`self._retriever` (already
company-scoped once §2 lands). **No changes needed to `generator.py` or
`validator.py` themselves** — company-awareness falls out automatically
from `Orchestrator` being instantiated per company.

## 7. API changes

`ChatRequest` gains a required field:

```python
class ChatRequest(BaseModel):
    company_id: str = Field(min_length=1, max_length=64, pattern=r"^[a-z0-9_]+$")
    session_id: str = Field(min_length=1, max_length=64)
    message: str = Field(min_length=1)
```

Every handler in `chat.py`, `compat.py`, `sessions.py`, `catalogue.py`
resolves the context once at the top:

```python
ctx = request.app.state.company_manager.get(body.company_id)  # 404 UnknownCompany if invalid
orchestrator = ctx.orchestrator
```

`app/api/legacy.py` (`POST /ask`, root-mounted, no `/api/v1` prefix): the
brief says "apply company resolution... to legacy endpoints if they remain
enabled" and separately "preserve backward compatibility where practical."
Recommended: add an *optional* `company_id` field defaulting to a
configurable `ELIARA_DEFAULT_COMPANY_ID` (e.g. `"beta"`), so the existing
deployed frontend (which sends no `company_id` today, per
`frontend-patch.md`) keeps working unmodified against Beta, while new
clients can pass `company_id` explicitly. This is the concrete mechanism
for "preserve backward compatibility where practical" — flagged for
explicit confirmation before implementation since it's a judgment call, not
purely mechanical.

`GET /sessions/{session_id}` and `POST /detect` need `company_id` added as
a query/body param respectively (currently pull straight off `app.state`).

## 8. Health

`GET /health/deep` becomes company-aware. Two reasonable shapes, to choose
during Milestone 6:

- `GET /health/deep?company_id=beta` — single-company deep health (minimal
  diff from today's shape, just resolves through `company_manager` instead
  of `app.state` directly).
- `GET /health/deep` (no param) — returns **all registered companies'**
  health in one response, e.g. `{"beta": {...}, "tire_guru": {...}}`, so an
  operator can see both at a glance. This directly satisfies the brief's
  "must be able to determine Beta health and Tire Guru health
  independently" and "a failure in Beta must not make Tire Guru appear
  healthy."

Recommendation: support both — no param returns the all-companies map,
`?company_id=` returns one company's detail unchanged in shape from today's
`DeepHealthResponse`. `data_boundaries()`'s hardcoded `fact_ai_sales_net`
(architecture doc §4) should also be fixed here: move the boundary-table
name into `CompanyConfig` (optional field, `None` = skip the boundaries
check gracefully) rather than hardcoding it in `ReadOnlyExecutor`.

## 9. Startup / shutdown lifecycle

`main.py`'s `lifespan()` shrinks to:

```python
registry = CompanyRegistry.from_file(settings.companies_config_path)
app.state.company_manager = CompanyContextManager(registry, settings, AnthropicClient(settings))
# Option A (matches brief's "lazy initialization"): build nothing yet,
#   first request per company builds+caches its context.
# Option B: eagerly build all registered companies at startup so the
#   first real request isn't slow, and so a broken company's DB is caught
#   at boot rather than on first use.
```

Recommendation: **eager build at startup, per company, each wrapped in its
own try/except** — directly satisfies the brief's "test startup when one
company database is unavailable... failure of one company must not
unnecessarily bring down the other." A company whose DB is missing/broken
at startup gets a context marked unhealthy (logged, and reflected in
`/health/deep`) rather than crashing the whole app or being silently
absent; a background retry (reusing the same "lazy `get()` retries if the
cached context is marked failed" logic) covers the case where the DB
appears later.

On shutdown: iterate `company_manager` and call `ctx.watcher.stop()` /
`ctx.executor.close()` for every built context (direct generalization of
today's single close/stop calls).

## 10. Concurrency / pool isolation

Because each `CompanyContext` owns its own `ReadOnlyExecutor` (own
`queue.Queue`-backed pool) and the pool is only ever accessed via that
specific instance, "Beta traffic must not consume Tire Guru's pool" is true
*by construction* — there is no shared pool to contend over. No new
locking is needed beyond what `ReadOnlyExecutor` already has internally.
The only shared mutable state across companies after this refactor is the
process-global `RateLimiter` and `metrics` dict (§2) and the `AuditTrail`
file-write lock (§3) — both already thread-safe today and both acceptable
to remain shared per the brief ("global settings should remain global").

## 11. What stays completely unchanged

`ReadOnlyExecutor`, `DatabaseWatcher`, `build_discovery`,
`build_entity_index`, `MetadataLoader`, `HybridRetriever`, `EmbeddingCache`
internals, `SQLGenerator`, SQL `validator.py`, `Orchestrator`'s internal
decision/routing logic, `ReasoningAgent` (`agent.py`, untouched per the
brief), `verification.py`, `attention_queue.py`, `execution/{aggregate,
formatter,refine,visual}.py`, the entire prompt-rendering mechanism in
`prompts/loader.py`, and all five playbook YAMLs' *content* (only their
directory location moves).

## 12. Open questions for MIGRATION_PLAN.md / user confirmation

1. Does a Tire Guru SQLite database already exist, or does this milestone
   need to also produce/receive one? The repo contains none.
2. Does Tire Guru's schema include equivalent registry/glossary
   tables/views (`chatbot_question_view_registry`,
   `vw_gold_business_glossary`/`batch_13_business_glossary`) that
   `MetadataLoader` expects, or does it degrade gracefully to "no
   registry/no glossary" mode? (`MetadataLoader` already tolerates missing
   individual objects per-object, and has a two-source glossary fallback —
   but has not been exercised against a schema with zero matching
   registry/glossary objects.)
3. Audit trail: one shared file/day vs. one directory per company (§3) —
   needs a decision.
4. Legacy `/ask` default-company behavior (§7) — needs explicit
   confirmation since it's a backward-compatibility judgment call.
5. Eager vs. lazy company-context build at startup (§9) — recommendation
   given, needs confirmation.
