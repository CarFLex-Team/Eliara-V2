# Deployment

## Cold start (any machine with Docker) — 4 commands

```bash
git clone https://github.com/Mosapmohamd/AI-Decision-Support-System.git && cd AI-Decision-Support-System
cp .env.example .env          # then set ELIARA_ANTHROPIC_API_KEY
# place the database at ./data/eliara_production_clean.db
docker compose up --build -d
```

Verify: `python scripts/smoke_test.py` → `SMOKE: PASS`.
Docs UI: http://<host>:8000/api/docs

The image bakes the bge-base-en-v1.5 model at build time — the running
container needs internet access ONLY for api.anthropic.com.

## Without Docker (dev / emergency)

```bash
pip install ".[ml]"
cp .env.example .env
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

## Configuration

Everything is env-driven with prefix `ELIARA_` (see `.env.example` and
`app/core/config.py`). Key knobs: `QUERY_TIMEOUT_S`, `MAX_ROWS`,
`TOP_K_VIEWS`, `CHAT_RATE_LIMIT_PER_MIN`, `RESULT_CACHE_TTL_S`,
`PAYLOAD_MAX_CHARS`, `AUDIT_ENABLED`, `AUDIT_DIR`.

## Database refresh (SAP B1 export)

Replace the file at the mounted path atomically (write to a temp name on the
same filesystem, then rename over the old file). Within
`ELIARA_DB_WATCH_INTERVAL_S` (default 60s) the platform detects the change,
reopens connections, rebuilds the metadata index, re-embeds only if the schema
fingerprint changed, and flushes the result cache. Zero downtime.

## Persistence map

| What | Where | Survives restart |
|---|---|---|
| Analytical DB | host `./data` (ro mount) | yes (externally managed) |
| Audit trail | host `./audit/audit-YYYY-MM-DD.jsonl` | yes |
| Embeddings cache | named volume `eliara-cache` | yes |
| Sessions / result cache / metrics | RAM | no (by design) |
