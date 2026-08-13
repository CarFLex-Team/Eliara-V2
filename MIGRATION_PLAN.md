# Eliara — Migration Plan (Milestone 1)

Sequenced to match the brief's Milestones 2–8. Each step is designed to
keep the test suite green and the app deployable at every intermediate
commit — no big-bang rewrite.

## Step 0 — Pre-work / decisions needed before coding starts

- Confirm the 5 open questions listed at the end of MULTI_COMPANY_DESIGN.md
  (Tire Guru DB availability/schema shape, audit layout, legacy `/ask`
  default company, eager-vs-lazy startup).
- Confirm `companies.yaml` (or equivalent) as the registry mechanism and
  its exact schema.

## Step 1 — Company Registry + Config (Milestone 2, part 1)

- Add `app/company/registry.py` (`CompanyConfig`, `CompanyRegistry`).
- Add `ELIARA_COMPANIES_CONFIG` setting to `Settings`.
- Add a `companies.yaml` with a **single `beta` entry** pointing at the
  existing `data/eliara_production_clean.db` — i.e. this step changes
  *where configuration lives*, not runtime behavior yet. App still uses
  the old single-context path in `main.py`.
- Tests: registry loads/validates a fixture YAML; unknown company_id
  raises the expected error.

Risk: low. No behavior change to any live endpoint.

## Step 2 — CompanyContext + CompanyContextManager (Milestone 2, part 2)

- Add `app/company/context.py` (`CompanyContext`, `CompanyContextManager`)
  per MULTI_COMPANY_DESIGN.md §2, built entirely from existing
  `ReadOnlyExecutor` / `build_discovery` / `build_entity_index` /
  `PromptManager` / `PlaybookLibrary` / `Orchestrator` / `DatabaseWatcher`.
- Wire `main.py`'s `lifespan()` to build one `CompanyContextManager` with
  the `beta`-only registry from Step 1, but **do not touch API routes
  yet**. `app.state.orchestrator` etc. can temporarily be set to
  `company_manager.get("beta").orchestrator` so existing routes keep
  working unmodified while this layer is proven out.
- Tests: `CompanyContextManager.get("beta")` returns a working context
  whose `.orchestrator.handle(...)` produces the same answers as today's
  direct `Orchestrator` construction (regression test against a known
  fixture question).

Risk: low-medium. This is the biggest new class, but it delegates to
already-tested components, and the app still behaves exactly as before
from the outside (single company, no API change).

## Step 3 — Multi-Database Runtime (Milestone 3)

- Add a **second** `companies.yaml` entry for `tire_guru` pointing at a
  test/fixture SQLite file (real Tire Guru DB if available by this point,
  otherwise a minimal synthetic fixture DB with a couple of tables/views —
  needed either way to write isolation tests before a real DB exists).
- Verify both contexts build independently: `CompanyContextManager.get
  ("beta")` and `.get("tire_guru")` both succeed, own separate
  `ReadOnlyExecutor` pools, and a query against one never touches the
  other's connection (assert via pool identity / mock).
- Confirm lazy-vs-eager startup decision (Step 0) is implemented.

Risk: medium — first point where two real SQLite files coexist; watch for
accidental path collisions (embedding cache dir, audit dir) called out in
CURRENT_ARCHITECTURE.md §5/§7.

## Step 4 — Multi-Company Discovery (Milestone 4)

- No new code expected beyond what Step 3 already exercises — discovery
  is already per-executor via `build_discovery`. This step is primarily
  **verification**: retrieval isolation tests (a Tire Guru question's
  candidate views never include Beta view names and vice versa), and
  confirming `MetadataLoader` degrades sanely if Tire Guru lacks the
  registry/glossary tables Beta has (open question #2).
- Fix the `ReadOnlyExecutor.data_boundaries()` hardcoded
  `fact_ai_sales_net` (CURRENT_ARCHITECTURE.md §4) — move the
  boundary-table/column into `CompanyConfig` as an optional field.

Risk: low, mostly test-writing — unless Tire Guru's real schema surfaces
gaps in `MetadataLoader`'s current graceful-degradation coverage, in which
case that module gets small, targeted fixes (not a rewrite).

## Step 5 — Prompts & Business Configuration (Milestone 5)

- Move `app/prompts/templates/` → `app/prompts/shared/templates/` (path
  move only; content unchanged, since no prompt currently hardcodes
  company data — CURRENT_ARCHITECTURE.md §7).
- Add `PromptManager.for_company()` factory (MULTI_COMPANY_DESIGN.md §4).
- Move the 5 existing playbook YAMLs → `companies/beta/playbooks/`; add
  `PlaybookLibrary.load(extra_dir)` (§5); create empty
  `companies/tire_guru/playbooks/`.
- Move `scan_views` out of global `Settings` into `CompanyConfig` (§1/§5);
  update `Orchestrator` to receive `scan_views` as a constructor param
  instead of reading `self._settings.scan_views`.

Risk: medium — touches the prompt loader's file-discovery path and the
playbook loader's constructor signature; both have existing test coverage
to update, not replace.

## Step 6 — Request Isolation (Milestone 6)

- Add `company_id` to `ChatRequest` (`app/api/v1/schemas.py`).
- Update `chat.py`, `compat.py`, `sessions.py`, `catalogue.py`,
  `chat.py`'s `/detect` to resolve `ctx = company_manager.get(body.
  company_id)` and use `ctx.*` instead of `request.app.state.*`.
- Decide + implement legacy `/ask` backward-compatibility behavior (open
  question #4).
- `RateLimiter` key becomes `f"{company_id}:{session_id}"`.
- `AuditTrail.record()` gains a required `company_id` parameter (per
  chosen design in open question #3).
- `GET /health/deep` becomes company-aware per MULTI_COMPANY_DESIGN.md §8.
- Now retire the Step-2 shim (`app.state.orchestrator = ...`) entirely —
  every route goes through `company_manager`.

Risk: highest single step — touches every API route and every test that
calls them. Recommend doing this file-by-file with the existing test suite
run after each file, not as one large commit.

## Step 7 — Regression + Isolation Testing (Milestone 7)

- Run full existing suite; fix any breakage from Step 6's request-shape
  change (tests will need `company_id` added to request bodies).
- Add the 16 isolation tests enumerated in the brief's "Tests" section
  (cross-company DB/cache/session/prompt/metadata/entity/playbook/scan/
  audit isolation; independent pools; refresh-doesn't-rebuild-other-company;
  concurrent Beta+Tire-Guru requests).

Risk: low-medium — mechanical test-writing against the now-stable
architecture from Steps 1–6.

## Step 8 — Production Validation (Milestone 8)

- Simultaneous-request test, one-company-refresh-while-other-serves test,
  graceful shutdown test, startup-with-one-DB-missing test (validates the
  Step 3/9 eager-build-with-per-company-try/except decision).
- Update `DEPLOY.md` / `.env` example / `docker-compose.yml` for the new
  `ELIARA_COMPANIES_CONFIG` + `companies.yaml` + `companies/` directory
  layout, keeping the existing Docker/mounted-SQLite deployment model
  (brief explicitly requires this — no Kubernetes, no Redis, no rewrite).

Risk: low — packaging/documentation, not new logic.

## Rollback strategy

Every step above lands as its own commit/PR and keeps the previous step's
external behavior intact until the *next* step explicitly changes it
(Step 6 is the one true breaking API change — everything before it is
additive). If Step 6 needs to be reverted, Steps 1–5's registry/context/
prompt/playbook groundwork stays in place and useful; only the API-facing
`company_id` requirement would need to roll back.

## Files that will change (by step)

| Step | New files | Modified files |
|---|---|---|
| 1 | `app/company/registry.py`, `companies.yaml` | `app/core/config.py` |
| 2 | `app/company/context.py` | `app/main.py` |
| 3 | (test fixtures only) | — |
| 4 | — | `app/execution/executor.py` (data_boundaries), possibly `app/discovery/metadata_loader.py` |
| 5 | `companies/beta/playbooks/*.yaml` (moved), `companies/tire_guru/playbooks/` (empty) | `app/prompts/loader.py`, `app/orchestrator/playbooks.py`, `app/core/config.py` (remove `scan_views`), `app/orchestrator/orchestrator.py` (scan_views param) |
| 6 | — | `app/api/v1/schemas.py`, `app/api/v1/chat.py`, `app/api/v1/compat.py`, `app/api/v1/sessions.py`, `app/api/v1/catalogue.py`, `app/api/v1/health.py`, `app/api/legacy.py`, `app/core/cache.py` (rate-limiter key), `app/core/audit.py` (company_id param), `app/main.py` (remove shim) |
| 7 | new test files under `tests/` | test fixtures for Tire Guru |
| 8 | — | `DEPLOY.md`, `.env` example, `docker-compose.yml` |

## Files that will remain unchanged

`app/execution/{executor.py (minus data_boundaries fix)/db_watcher.py}`,
`app/discovery/*` (minus targeted MetadataLoader fixes if needed),
`app/sqlgen/*`, `app/orchestrator/{orchestrator.py core logic, agent.py,
agent_models.py, decision_models.py, verification.py, conversation.py}`,
`app/detection/attention_queue.py`, `app/execution/{aggregate, formatter,
refine, visual}.py`, `app/llm/anthropic_client.py`, all prompt YAML
*content*, all playbook YAML *content*.
