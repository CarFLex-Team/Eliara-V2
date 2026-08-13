# Runbook

First stop for any incident: `GET /api/v1/health/deep` and the JSON logs
(one `chat_request_complete` line per request carries decision, view, sql,
cache, latency, tokens).

## Symptom → action

**503 "AI service temporarily unavailable"** — Anthropic API unreachable or
rate-limited. The client already retried with backoff. Check api key validity,
Anthropic status page, and account limits. No restart needed; recovers alone.

**deep health: database=not_found** — DB file missing at `ELIARA_DB_PATH`.
Restore the file; the watcher picks it up only at startup if it was missing —
restart the container after restoring.

**deep health: metadata_index=not_built** — startup couldn't read metadata.
Check logs for `metadata_loaded` / errors; usually a corrupt or partial DB
copy. Re-copy the DB atomically.

**Retrieval quality drops after a refresh** — run the permanent gate:
`python scripts/eval_retrieval.py --db data/eliara_production_clean.db`
(top-3 ≥ 95%). Misses are printed per question.

**Slow answers** — profile is in the logs: intent ~2s, execution <1s, answer
generation dominates (~10-14s for rich answers). Reduce answer length via a
new `orchestrator_answer` prompt version, or lower `PAYLOAD_MAX_CHARS`.

**429 for a user** — per-session sliding window. Raise
`ELIARA_CHAT_RATE_LIMIT_PER_MIN` if legitimate.

**Corrupt embeddings cache** — delete the `eliara-cache` volume (or the
`emb_*.npz` files); rebuilt automatically on next start (~seconds on CPU).

**Rotate the API key** — update `.env`, `docker compose up -d` (recreates the
container). Keys never appear in logs (redaction test-enforced).

**Audit questions ("who asked X?")** — `./audit/audit-YYYY-MM-DD.jsonl`, one
JSON object per request: ts, session, question, routing, generated SQL, answer,
tokens. Archive/rotate by date file; the app only ever appends to today's file.

## What can never happen (test-enforced)

Writes to the DB (mode=ro + authorizer + AST gate), non-SELECT execution,
internal details in user-facing errors, secrets in logs.
