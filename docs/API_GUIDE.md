# Eliara API — Frontend Integration Guide

The API now serves multiple companies from one backend. The one thing that
changes for every existing integration: **every request needs a
`company_id`.** Everything else about the contract is the same as before.

Base URL: wherever the backend is deployed (e.g. `https://your-domain/api/v1`,
or `http://localhost:8000/api/v1` locally). Registered company IDs today:
`beta`, `tire_guru`.

---

## POST /api/v1/chat

The main endpoint. Ask a question, get a full answer back in one response.

**Request**
```json
{
  "company_id": "beta",
  "session_id": "user-session-abc123",
  "message": "top customers?"
}
```
- `company_id` — required. Which company's data to query. Must be a
  registered company id (lowercase, letters/digits/underscore only).
- `session_id` — required. Any string that identifies this conversation
  thread (e.g. a UUID you generate per chat window). Conversation history
  is scoped by `company_id` + `session_id` together — the same
  `session_id` under two different companies is treated as two completely
  separate conversations.
- `message` — required, non-empty.

**Response**
```json
{
  "answer": "Beta Motors leads with $8,000 in lifetime revenue...",
  "meta": {
    "view_used": "vw_q002_top_10_customers_by_lifetime_revenue",
    "sql_generated": false,
    "cache_hit": false,
    "latency_ms": 1200,
    "input_tokens": 340,
    "output_tokens": 85,
    "source": "data"
  }
}
```
- `meta.source` is `"data"` for anything answered from the company's own
  database, or `"external"` for a `/search`-prefixed question (general
  knowledge, no governed company data). Use this to badge external answers
  in the UI rather than re-detecting the `/search` prefix client-side.

**Errors** — see the Errors section below.

---

## POST /api/v1/chat/stream

Same pipeline as `/chat`, delivered as Server-Sent Events instead of one
JSON blob. Same request body (`company_id`, `session_id`, `message`).

Each event is a line `data: <json>\n\n`. Event shapes, in order:

```
{"type": "stage", "value": "Querying your data..."}   // optional, transient loading text
{"type": "token", "value": "Beta Motors"}              // one or more — append to build the answer
{"type": "token", "value": " leads with..."}
{"type": "visual", "value": {...}}                     // optional, only when a chart applies
{"type": "done"}                                        // always last on success — no payload
```

On failure mid-stream, `{"type": "error", "detail": "..."}` appears in
place of `done`.

Pre-flight checks (unknown company, rate limit, message too long) happen
**before** the stream opens, so those still come back as ordinary HTTP
error responses (see Errors below), not as an `error` event. If you got a
200 with an event stream at all, the request was accepted and is running.

---

## GET /api/v1/sessions/{session_id}

Fetch the conversation history for one session (mainly a debugging aid).

```
GET /api/v1/sessions/user-session-abc123?company_id=beta
```
`company_id` is a required query parameter here (not part of the path).

```json
{
  "company_id": "beta",
  "session_id": "user-session-abc123",
  "messages": [
    {"role": "user", "content": "top customers?"},
    {"role": "assistant", "content": "Beta Motors leads..."}
  ]
}
```

---

## GET /api/v1/health/deep

Not typically called by the frontend directly, but useful for an admin/
status page.

- `GET /api/v1/health/deep` — no params — returns **every** registered
  company's status in one response:
  ```json
  {
    "status": "ok",
    "requests_total": 42,
    "cache_hits": 10,
    "companies": {
      "beta": {"status": "ok", "database": "ok", "metadata_index": "ok (3 objects, hybrid/bge)", "llm": "key_configured", "requests_total": 0, "cache_hits": 0, "last_db_refresh": null},
      "tire_guru": {"status": "ok", "database": "ok", "metadata_index": "ok (191 objects, hybrid/bge)", "llm": "key_configured", "requests_total": 0, "cache_hits": 0, "last_db_refresh": null}
    }
  }
  ```
- `GET /api/v1/health/deep?company_id=beta` — narrows to one company's
  detail, same shape as one entry in `companies` above.

A company's `status` can be `"ok"` or `"degraded"` independently of every
other company — one company being down never affects another's reported
status.

---

## Errors

Every error response is JSON with a single `error` field containing a
message safe to show the user directly:

```json
{"error": "Unknown company."}
```

| Status | Meaning | When |
|---|---|---|
| 404 | Unknown company | `company_id` isn't a registered company |
| 422 | Message too long | over the server's message-length limit |
| 429 | Rate limited | too many requests for this company+session in the current window |
| 503 | Company temporarily unavailable | that company's backend context failed to start (e.g. its database is unreachable) — other companies are unaffected |
| 500 | Something else went wrong | generic internal error; message is safe to display but won't have specifics |

None of these ever include a stack trace, SQL text, or internal file
paths — the `error` string is always safe to render as-is.

---

## Migration note for existing integrations

If you're updating code that talked to this API before the multi-company
change: the only breaking change is that `company_id` is now a required
field on `/chat`, `/chat/stream`, and a required query param on
`/sessions/{id}`. Everything else — response shapes, SSE event format,
error format — is unchanged. Pick which company's data your UI should show
(likely a fixed value per deployment, e.g. a Beta-branded frontend always
sends `"company_id": "beta"`) and add that one field everywhere.
