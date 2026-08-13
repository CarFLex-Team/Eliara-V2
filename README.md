# Eliara Analytics Platform

Read-only AI business analytics platform for Beta Company. Management asks
business questions in natural English; the system answers from the Eliara
analytical SQLite database (refreshed from SAP Business One) — preferring
curated analytical views, with validated LLM-generated SQL as fallback.

## Architecture (summary)

- **FastAPI** API layer (`/api/v1`)
- **Claude Sonnet 4.6** — orchestrator: intent, routing, final answers
- **Claude Haiku 4.5** — SQL generation only (SELECT-only, AST-validated)
- **View Discovery Engine** — bge-base-en-v1.5 embeddings + BM25, RRF fusion
- **Read-only execution** — `mode=ro` + SQLite authorizer + timeouts

See `docs/` (added in M8) for deployment and runbook.

## Quickstart (dev)

```bash
pip install ".[dev]"
cp .env.example .env        # fill ELIARA_ANTHROPIC_API_KEY
pytest -q
uvicorn app.main:app --reload
# → http://127.0.0.1:8000/api/docs
```

## Docker

```bash
docker compose up --build
```

## Status

All milestones delivered (M0-M8): scaffold, read-only execution (triple
defense), discovery engine (100% top-1 on the 90-question gate), LLM client +
versioned prompts, orchestrator, SQL fallback behind an AST validation gate,
follow-up resolution, hardening (cache, rate limit, token budgets), audit
trail, deployment package. 160+ tests.

Docs: `docs/DEPLOYMENT.md` · `docs/RUNBOOK.md` · `docs/PROMPTS.md` ·
`docs/HARDENING_CHECKLIST.md`
