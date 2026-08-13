# M7 Hardening Checklist

## Security (verified by tests)
- [x] Writes physically impossible: `mode=ro` connections (test_write_impossible_even_without_authorizer)
- [x] Authorizer denies all non-read actions; only introspection pragmas whitelisted
- [x] 32 forbidden SQL statements rejected by the AST validator golden suite
- [x] Executed SQL regenerated from validated AST — model text never runs
- [x] No-op (zero-table) queries rejected
- [x] Zero-leak error responses: public messages never contain SQL, table names,
      stack traces, or internal reasons (asserted in security suite)
- [x] Filter values parameter-bound; injection probes inert
- [x] Haiku isolation: raw user message never reaches sqlgen prompts (tested)
- [x] Prompt-injection framing in intent system prompt (user message = data)
- [x] API key SecretStr; log processor redacts *key*/*secret*/*token* fields
- [x] SAP reference + batch_* layers structurally outside index and whitelist
- [x] Per-session rate limiting (429), message length cap (422)

## Reliability
- [x] LLM retry policy: backoff on 429/5xx/connection, fail-fast otherwise
- [x] Structured-output corrective retry (JSON) + SQL corrective retry
- [x] DB refresh: pool reopen + atomic index swap + result-cache flush
- [x] Query timeout + row cap on every execution
- [x] Watcher survives failing callbacks

## Performance / cost
- [x] Result cache (TTL, flushed on refresh) for views and generated SQL
- [x] Embeddings disk cache keyed by schema fingerprint
- [x] Intent token budget: full columns only for top-3 candidates (cap 15)
- [x] Answer payload budget: configurable char cap (default 6000)

## Observability
- [x] One closing log line per request: decision, view, sql, cache, latency, tokens
- [x] prompt name@version on every LLM log line
- [x] /health/deep: data boundary, index size+mode, key presence, counters,
      last DB refresh timestamp
