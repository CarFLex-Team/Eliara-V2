# Eliara — Current Architecture (Milestone 1 Analysis)

Based on direct inspection of the supplied codebase (not prior assumptions).

## 1. Composition root: `app/main.py`

Everything company-scoped today is built **once**, at process startup, inside
the `lifespan()` context manager, and hung off `app.state`:

```
app.state.executor          ReadOnlyExecutor(settings.db_path)
app.state.metadata_index    MetadataIndex   (from build_discovery)
app.state.retriever         HybridRetriever (from build_discovery)
app.state.entities          EntityIndex     (from build_entity_index)
app.state.result_cache      ResultCache
app.state.conversations     InMemoryConversationStore
app.state.orchestrator      Orchestrator(...)
app.state.db_watcher        DatabaseWatcher(settings.db_path)
app.state.rate_limiter      RateLimiter          (NOT company-scoped — see §6)
app.state.metrics           dict                 (NOT company-scoped — see §6)
```

`_on_db_refresh()` (a closure inside `lifespan`) rebuilds index/retriever/
entities and atomically swaps them onto `app.state` and onto the single
`orchestrator` instance. This is the *only* refresh/atomic-swap mechanism
that exists, and it assumes one executor / one set of indexes.

**This is the single biggest structural fact**: there is exactly one of
everything, and it is a module-level/app-level singleton, not something
resolved per request.

## 2. Request flow

`app/api/v1/chat.py` (`POST /chat`, `POST /chat/stream`) and
`app/api/legacy.py` (`POST /ask`) all do:

```python
orchestrator = request.app.state.orchestrator   # the one global instance
outcome = await orchestrator.handle(session_id, message)
```

No request carries a company/tenant identifier anywhere in the current
schemas (`ChatRequest` = `{session_id, message}` only).

## 3. `Orchestrator` (`app/orchestrator/orchestrator.py`, 1183 lines)

Already constructor-injected (good news for refactoring — it does **not**
reach into globals internally, with one exception):

```python
Orchestrator(retriever, index, executor, prompts, conversations, llm,
             settings, result_cache=None, audit=None)
```

- `self._sqlgen = SQLGenerator(llm, prompts, settings)` — built internally
  from injected deps, fine.
- `self._playbooks = PlaybookLibrary.load()` — **loads from a hardcoded
  module-relative path** `app/orchestrator/definitions/` (see §7). Not
  injected, not swappable per company today.
- `self._agent: ReasoningAgent | None = None` — built lazily from
  `self._executor` / `self._retriever` etc., so it inherits whatever
  company scope the Orchestrator itself has. No separate global state.
- Result cache keys (`_cached_run`) are constructed by the *caller* inside
  Orchestrator methods, currently as `(decision, view_or_sql, params, ...)`
  tuples — **no company_id component anywhere**, and no such field exists to
  add one to today.
- `_glossary_for()` reads `self._index.glossary` — glossary is already
  per-executor/per-database (see §8), so this one is naturally company-safe
  once `self._index` is company-scoped.
- `scan_views` (used at line ~681) is read from `self._settings.scan_views`
  — a **global `Settings` field** (`app/core/config.py`), i.e. one fixed
  list of view names shared by every request regardless of which database
  is being queried.

## 4. Database layer

`ReadOnlyExecutor` (`app/execution/executor.py`) is already a clean,
self-contained, per-database class — constructed with one `db_path`, owns
its own connection pool (`queue.Queue`), has `reopen()`/`close()`. It has
**no cross-instance shared state**, which makes it easy to have N instances
side-by-side (one per company). Its read-only defenses (URI `mode=ro`,
SQLite authorizer, progress-handler timeout, row cap) are per-connection and
need no changes.

One important finding: `data_boundaries()` hardcodes a table name and
column that are specific to the current (Beta) production schema:

```python
"SELECT MIN(posting_date_iso), MAX(posting_date_iso) FROM fact_ai_sales_net"
```

This is a business-specific assumption baked into an otherwise-generic
component and will silently fail (caught, returns `None`) against a
database that doesn't have `fact_ai_sales_net`. Not fatal, but a real
single-company assumption to note for §"problems found" below.

`DatabaseWatcher` (`app/execution/db_watcher.py`) is also already a clean
per-path class (`DatabaseWatcher(db_path, interval_s)`, `.on_change()`
callback list, `.check_once()`). No shared state. The only "global" part is
that `main.py` currently creates exactly one.

## 5. Discovery layer

`build_discovery(executor, settings)` (`app/discovery/service.py`) is a pure
function: executor in, `(MetadataIndex, HybridRetriever)` out. No hidden
globals. `build_entity_index(executor, objects, ...)` is the same shape.

**Metadata is entirely database-derived, not file-based.** `MetadataLoader`
(`app/discovery/metadata_loader.py`) reads `sqlite_master`, a
`chatbot_question_view_registry`-style registry table, and a glossary view
(`vw_gold_business_glossary` / `batch_13_business_glossary`) — all *from
the target SQLite file itself*. There are no `schema.json` /
`entities.json` files anywhere in the repo. This matches the refactor
brief's contingency: "if metadata is intentionally loaded from the database
itself, preserve that behavior" — it is, and this makes company metadata
essentially free once each company has its own `ReadOnlyExecutor` pointed
at its own `.db` file. Tire Guru does **not** need hand-authored schema
files unless its database lacks the same registry/glossary
tables/views Beta's does (open question — see MIGRATION_PLAN.md).

`EmbeddingCache` (`app/discovery/embedder.py`) is constructed from
`settings.embedding_cache_dir` — currently one shared directory
(`data/cache`). Needs a per-company subdirectory or the two companies'
cached embeddings will collide/overwrite each other on disk.

## 6. Cross-cutting global state (not company-aware today)

| Component | File | Scope today | Company-aware? |
|---|---|---|---|
| `RateLimiter` | `app/core/cache.py` | one instance, keyed by `session_id` | No — two companies sharing a `session_id` would share a quota |
| `ResultCache` | `app/core/cache.py` | one instance, keyed by caller-built tuple | No — key has no company component |
| `AuditTrail` | `app/core/audit.py` | one instance, one `audit_dir`, records have no `company_id` field | No |
| `InMemoryConversationStore` | `app/orchestrator/conversation.py` | one instance, keyed by `session_id` only | No — `beta`+`session=abc` and `tire_guru`+`session=abc` are literally the same dict key today |
| `app.state.metrics` | `main.py` | one dict (`requests_total`, `cache_hits`) | No |
| `Settings` (`get_settings()`) | `app/core/config.py`, called directly in 7+ files (`chat.py`, `compat.py`, `health.py`, `legacy.py`) | one process-wide `@lru_cache` singleton | No — and it currently holds several fields that are semantically company-specific: `db_path`, `scan_views`, `answer_char_budget`, `verification_strict`, etc. |

## 7. File-based, hardcoded-path resources

| Resource | Path | Notes |
|---|---|---|
| Playbooks | `app/orchestrator/definitions/*.yaml` (5 files: `investigate_customer`, `procurement_plan`, `supplier_review`, `stock_action_plan`, `business_review`) | Loaded once via `PlaybookLibrary.load()` from a hardcoded `Path(__file__).parent / "definitions"`. Every playbook step names real Beta view names (e.g. `stock_action_plan.yaml` drives the `/scan` prefix's `scan_views` list) — these are Beta-specific by construction and won't resolve against Tire Guru's schema. |
| Prompts | `app/prompts/templates/{orchestrator,agent,sqlgen,external}/*.yaml` | Loaded once via `PromptManager()._load_all()` from a hardcoded `Path(__file__).parent / "templates"`, recursively. **On inspection, none of the prompt YAMLs contain a hardcoded company name or hardcoded view names** — they're templated with Jinja2 variables (`schema_context`, `glossary`, candidate views, etc.) supplied at render time from the per-request/per-executor data. This is good: the prompt *system* is already mostly company-neutral; only the *data fed into it* (schema, glossary, candidate views) is company-specific, and that already flows through function arguments, not hardcoded text. |
| `scan_views` | `app/core/config.py` (`Settings.scan_views`, a hardcoded Python list of 7 Beta view names) | The one clear hardcoded-business-data leak into global `Settings` that the refactor brief specifically calls out. |
| Embedding cache | `settings.embedding_cache_dir` (default `data/cache`) | One shared directory today. |
| Audit files | `settings.audit_dir` (default `audit/`) | One shared directory, one file per *day* (not per company). |
| DB path | `settings.db_path` (default `data/eliara_production_clean.db`) | One path, one env var (`ELIARA_DB_PATH`). |

## 8. API surface (`app/api/`)

- `app/api/v1/chat.py` — `POST /chat`, `POST /chat/stream`, `POST /detect`. All three pull `request.app.state.orchestrator` / `.executor` directly.
- `app/api/v1/sessions.py` — `GET /sessions/{id}` — pulls `request.app.state.conversations`, keyed by `session_id` only.
- `app/api/v1/catalogue.py` — pulls discovery objects off `app.state` (not yet read in full; needs Milestone 4 pass).
- `app/api/v1/health.py` — `GET /health`, `GET /health/deep` — deep health reports on the *one* executor/index/watcher on `app.state`; **hardcodes `fact_ai_sales_net` indirectly via `executor.data_boundaries()`** (see §4).
- `app/api/v1/compat.py` — legacy response-contract translation for the old frontend; calls `get_settings()` directly and the shared orchestrator.
- `app/api/legacy.py` (356 lines, mounted at **root**, not under `/api/v1`) — `POST /ask`, `GET /health`, has its own shared-secret gate (`ask_shared_secret`) and its own chart-building logic (`app/execution/visual.py`). Also reads `get_settings()` directly in three places.

None of these currently accept or resolve a `company_id`.

## 9. Reasoning Agent (`app/orchestrator/agent.py`, 474 lines)

Disabled by default (`agent_enabled = False`). Built lazily by
`Orchestrator` from `self._executor` / `self._retriever` / `self._index` /
`self._prompts` — i.e. it already only ever sees whatever the Orchestrator
instance was constructed with. **No separate global state of its own.**
Once `Orchestrator` itself is company-scoped, the agent is company-scoped
for free. Per the refactor brief, its internals are not to be touched.

## 10. Tests

Test suite is extensive (300+ tests per prior project history) and, by
construction, currently exercises the single global `app.state.*`
singletons via `create_app()` / `TestClient`. Expect most integration-level
tests (API-level) to need a `company_id` added to request bodies; unit
tests that construct `ReadOnlyExecutor`, `Orchestrator`, `PlaybookLibrary`,
etc. directly should mostly be unaffected since those classes are already
constructor-injected. (Full enumeration deferred to Milestone 7 per the
brief — "do not modify code until analysis is complete.")

## 11. Summary of single-company assumptions found

1. `main.py` builds exactly one of everything and hangs it on `app.state`.
2. `Settings` (global, `@lru_cache`) mixes platform-level fields
   (`log_level`, `chat_rate_limit_per_min`) with company-level fields
   (`db_path`, `scan_views`, arguably `answer_char_budget`/
   `verification_strict`/`playbooks_enabled` are debatable — see design doc).
3. `PlaybookLibrary.load()` reads one hardcoded directory of Beta-specific
   YAML files.
4. `scan_views` is a hardcoded Python list of Beta view names inside global
   `Settings`.
5. `InMemoryConversationStore` keys sessions by `session_id` alone —
   collision risk across companies.
6. `ResultCache` keys have no company component.
7. `RateLimiter` keys by `session_id` alone.
8. `AuditTrail` records have no `company_id` field; one shared directory.
9. `ReadOnlyExecutor.data_boundaries()` hardcodes a Beta table/column name.
10. `EmbeddingCache` uses one shared directory.
11. No request schema anywhere carries `company_id`.

## 12. What is *already* company-ready (good foundation)

- `ReadOnlyExecutor`, `DatabaseWatcher`, `build_discovery()`,
  `build_entity_index()` are all pure, constructor-injected, side-effect-free
  functions/classes with no shared global state — they can be instantiated
  N times, once per company, with no changes to their own code.
- `Orchestrator` takes all its dependencies through its constructor already
  — no internal `get_settings()`/global lookups except `PlaybookLibrary.load()`.
- The prompt YAML files are already free of hardcoded company data; company
  context is supplied at render time via Jinja2 variables.
- Metadata (schema, registry, glossary) is derived live from each
  database, so a second company's metadata requires no hand-authored files
  — just a second `.db` file with equivalent registry/glossary
  tables/views (or graceful absence handling, already partially present via
  `MetadataLoader`'s try/except per-object and its two-source glossary
  fallback).
