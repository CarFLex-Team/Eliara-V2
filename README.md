# Eliara Analytics Platform

Read-only AI business analytics platform serving **multiple companies** from
one deployment. Anyone asks business questions in plain English; the system
answers from that company's own analytical SQLite database (refreshed from
SAP Business One) — preferring curated analytical views, with validated
LLM-generated SQL as fallback.

Every request is scoped by `company_id`. Each registered company gets a
fully isolated runtime: its own database connection pool, discovery index,
entity index, prompts, playbooks, result cache, conversation store, and
audit trail. See [`MULTI_COMPANY_DESIGN.md`](MULTI_COMPANY_DESIGN.md) for
the full design and [`companies.yaml`](companies.yaml) for the current
company registry.

**Companies currently registered:**

| Company | Status |
|---|---|
| Beta | Full — playbooks, scan views, boundaries table configured |
| Tire Guru | **Partial** — chat and discovery work against its real database, but no curated playbooks, no scan views, no business glossary yet. See [issue #22](https://github.com/Mosapmohamd/Eliara-V2/issues/22). |

## Architecture (summary)

- **FastAPI** API layer (`/api/v1`)
- **`CompanyContextManager`** (`app/company/`) — resolves `company_id` to an
  isolated runtime context; builds every registered company eagerly at
  startup, each in its own try/except so one company's failure can't take
  down another or the process
- **Claude Sonnet** — orchestrator: intent, routing, final answers
- **Claude Haiku** — SQL generation only (SELECT-only, AST-validated)
- **View Discovery Engine** — bge-base-en-v1.5 embeddings + BM25, RRF
  fusion, built per company from that company's own database
- **Read-only execution** — `mode=ro` + SQLite authorizer + timeouts, one
  connection pool per company

See [`CURRENT_ARCHITECTURE.md`](CURRENT_ARCHITECTURE.md) for a full module
breakdown, and `docs/` for deployment and runbook material.

## Quickstart (dev)

```bash
pip install ".[dev]"
cp .env.example .env        # fill in ELIARA_ANTHROPIC_API_KEY
pytest -q
uvicorn app.main:app --reload
# -> http://127.0.0.1:8000/api/docs
```

Every request needs a `company_id` — see
[`docs/API_GUIDE.md`](docs/API_GUIDE.md) for the full API contract, or:

```bash
curl -X POST localhost:8000/api/v1/chat \
  -H 'Content-Type: application/json' \
  -d '{"company_id":"beta","session_id":"s1","message":"top customers?"}'
```

### Adding a company

1. Place its SQLite database at `data/companies/<id>/<id>.db`.
2. Add an entry to `companies.yaml` (`display_name` and `db_path` are the
   only required fields).
3. Restart — the new company's context is built automatically at startup.

No other code changes are required. See `MULTI_COMPANY_DESIGN.md` §1 for
the full `CompanyConfig` schema.

## Docker

```bash
docker compose up --build
```

Mounts `companies.yaml`, `companies/`, and each company's database per
`companies.yaml`'s paths. See [`DEPLOY.md`](DEPLOY.md) for the full
deployment walkthrough including the legacy-frontend contract.

## Status

Multi-company refactor complete (registry/context, multi-database runtime,
per-company discovery, prompts/business config, request isolation,
regression + isolation testing, production validation) with 405+ tests
passing and CI green on every change. See `MIGRATION_PLAN.md` for how the
refactor was sequenced, and the repo's open issues for known gaps
(Tire Guru completeness, a handful of answer-quality fixes, and process
items — issues are the source of truth, not this file).

Docs: [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md) ·
[`docs/RUNBOOK.md`](docs/RUNBOOK.md) · [`docs/PROMPTS.md`](docs/PROMPTS.md) ·
[`docs/HARDENING_CHECKLIST.md`](docs/HARDENING_CHECKLIST.md) ·
[`docs/API_GUIDE.md`](docs/API_GUIDE.md) ·
[`DEPLOY.md`](DEPLOY.md) (legacy-frontend go-live notes)

## Contributing

Every change goes through an issue and a pull request — no direct pushes to
`main`. See [issue #18](https://github.com/Mosapmohamd/Eliara-V2/issues/18)
for the standing agreement on this.
