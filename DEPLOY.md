# Go-live: new backend behind the existing website

Everything below is applied to the code already. `179 tests pass` (168 original
+ 11 new frontend-contract tests).

## What the frontend actually does

From `virtual-assistant-master/utils/api.ts`:

```ts
const res = await fetch("https://api.eliaracarflex.cfd/ask", {
  method: "POST",
  headers: { "Content-Type": "application/json", Accept: "*/*" },
  body: JSON.stringify({ message: question }),
});
if (!res.ok) throw new Error(`HTTP ${res.status}`);
```

That single line dictates the whole integration:

| Fact | Consequence |
|---|---|
| Hardcoded absolute URL | The browser calls the backend **directly**. CORS is mandatory, not optional. |
| Path is `/ask` | `/api/v1/chat` and `/api/v1/chat/compat` are never called. |
| Body is `{message}` only | No `session_id` — the new `ChatRequest` would have 422'd every request. |
| Throws on `!res.ok` | Any non-200 shows a generic "error while fetching" bubble, hiding the real reason. Errors must be **200 + `status:"error"`**. |

And from `ChatWindow.tsx` / `MessageBubble.tsx`:

```ts
if (!aiData || aiData.status === "error")  -> ErrorBubble(aiData.error.code, aiData.error.message)
else  content = aiData.answer, visual = aiData.visual || null

switch (msg.visual?.type?.toLowerCase())   // trend | table | ranking | distribution | clarification
```

So `error` must be an **object** `{code, message}`, `status` must never be the
string `"error"` on success, and `visual` unlocks real table/chart rendering
that the previous `compat.py` hardcoded to `null`.

## Changes applied

| File | Change |
|---|---|
| `app/api/legacy.py` | **New.** `POST /ask`, `POST /v1/query`, `GET /health`, `/stats`, `/recent` at root, in the exact old payload shape. |
| `app/main.py` | `CORSMiddleware` + mounts the legacy router. |
| `app/core/config.py` | `cors_origins`, `legacy_api_enabled`, `legacy_api_key`. |
| `app/orchestrator/orchestrator.py` | Keeps the `QueryResult` (`_last_result`) instead of discarding it — needed for `rows`/`columns`/`visual`. |
| `docker-compose.yml` | `container_name: eliara-api`, CORS env passthrough. |
| `.dockerignore` | **New.** Was absent; the `.db` and `.env` were going into the build context. |
| `tests/integration/test_legacy_frontend_contract.py` | **New.** 11 tests, each asserting one line of the frontend. |

### Visuals now work

`/ask` derives a `visual` from the result set the answer was written from:

- 2 columns + numeric second + ≤15 rows → `ranking` (bar chart)
- otherwise → `table`

Table cells are formatted as non-empty strings on purpose. `TableView.tsx`
renders `row[i] || row[col] || ""`, so a numeric `0` or a `NULL` would render as
a blank cell; `"0"` and `"—"` stay visible.

Sample response for *"Who are the top customers by lifetime revenue?"*:

```json
{
  "status": "success",
  "answer": "Beta Motors leads with AED 8,000 …",
  "domain": "customer",
  "endpoint_used": "vw_q002_top_10_customers_by_lifetime_revenue",
  "visual": {
    "type": "table",
    "title": "Top 10 Customers By Lifetime Revenue",
    "columns": ["customer_code", "customer_name", "lifetime_revenue", "lifetime_gross_profit"],
    "rows": [["C002", "Beta Motors", "8,000", "3,200"], ["C001", "Alpha Trading", "7,600", "2,400"]]
  },
  "data_as_of_date": "2026-06-27", "freshness_status": "stale",
  "error": null
}
```

## Why the tunnel failed

Not the token, and not CORS. Your old API container was named **`eliara-api`**
(old `DEPLOYMENT.md` §9). The new compose named the service `eliara` with no
`container_name`. A token-based Cloudflare tunnel takes its ingress from the
**Zero Trust dashboard**, not from your compose file — so
`api.eliaracarflex.cfd` was still routed to `http://eliara-api:8000`, which no
longer existed on the Docker network. cloudflared connected fine and returned
502 on every request before FastAPI ever saw it.

Fixed by pinning `container_name: eliara-api`. Verify:

```bash
docker compose exec tunnel wget -qO- http://eliara-api:8000/health || echo "NAME DOES NOT RESOLVE"
```

If you'd rather rename properly, change the service in
Zero Trust → Networks → Tunnels → your tunnel → Public Hostname to
`http://eliara:8000` and drop the `container_name`.

## Before you run: three things

**1. Rotate the leaked credentials.** The Anthropic key and Cloudflare tunnel
token were committed inside the first zip. Rotate both — the tunnel token is the
serious one, it can be used to open a tunnel into your network.

**2. Fix `.env`.** The variable is read with the `ELIARA_` prefix. Your current
`.env` has a bare `CORS_ORIGINS=…`, which `Settings(extra="ignore")` silently
discards. Also drop the dead `USE_CLAUDE_BRAIN` / `ELIARA_PROSE_ENABLED`, which
belonged to the old stack:

```bash
ELIARA_ENVIRONMENT=prod
ELIARA_ANTHROPIC_API_KEY=sk-ant-...        # the NEW rotated key
ELIARA_CORS_ORIGINS=*                       # safe: no cookies cross-origin
ELIARA_DEFAULT_COMPANY_ID=beta              # legacy /ask defaults here when no company_id is sent
ELIARA_BETA_DB_HOST_PATH=./data/companies/beta/beta.db
ELIARA_TIRE_GURU_DB_HOST_PATH=./data/companies/tire_guru/tire_guru.db
CLOUDFLARE_TUNNEL_TOKEN=...                 # the NEW rotated token
```

`*` is safe here because the frontend sends no credentials, and `allow_credentials`
is `False`. Pin it to your Vercel origin once the domain is final.

**3. Put both databases in place first, and check `companies.yaml`.** The
platform now serves two companies from one process — see
`MULTI_COMPANY_DESIGN.md` for the full design. `companies.yaml` at the repo
root is the single source of truth for which companies exist; each entry's
`db_path` is resolved relative to the container's `/srv/eliara` working
directory. The compose defaults expect:

```
./data/companies/beta/beta.db
./data/companies/tire_guru/tire_guru.db
```

**If either file does not exist when you run `docker compose up`, Docker
creates an empty *directory* with that name** and mounts it; that company's
`ReadOnlyExecutor` then fails to open at startup. This is caught (per-company
eager build with its own try/except — see `app/company/context.py`) so it
will NOT crash the other company or the process, but that company's
`/api/v1/health/deep` entry will show `status: "degraded"`. Check first:

```bash
ls -l eliara_master.db && file eliara_master.db   # must say "SQLite 3.x database"
```

If you get `unable to open database file` with a valid file, the DB is in WAL
mode; checkpoint it on the host before deploying:

```bash
sqlite3 eliara_master.db "PRAGMA journal_mode=DELETE; VACUUM;"
```

## Deploy

```bash
docker compose --profile tunnel up -d --build

curl -s localhost:8000/health | jq '.status, .v4_engines.database'

# the exact request the browser makes
curl -s -X POST localhost:8000/ask \
  -H 'Content-Type: application/json' \
  -d '{"message":"Who are the top 10 customers by lifetime revenue?"}' \
  | jq '{status, domain, row_count, visual: .visual.type}'

# CORS preflight
curl -s -i -X OPTIONS localhost:8000/ask \
  -H "Origin: https://your-app.vercel.app" \
  -H "Access-Control-Request-Method: POST" | grep -i access-control

# through the tunnel
curl -s https://api.eliaracarflex.cfd/health
```

Then open the site and send a message. If steps 2–3 pass locally but the site
is blank, it's the tunnel (step 5), not the application.

## One real limitation — follow-ups

The frontend sends **no conversation id**. The new system's follow-up resolution
("sort them by margin", "what about 2024?") depends on `session_id`, so without
one every message is a fresh conversation.

Stopgap in place: `/ask` derives a stable key from `CF-Connecting-IP` +
`User-Agent` (the real browser IP, since `request.client.host` behind the tunnel
would be the tunnel itself and would merge every user into one shared history).
This works, but two users on the same office NAT with the same browser share a
thread.

The proper fix is one line in the frontend — the thread id already exists in
`useThreadStore`. See `frontend-patch.md`. Ship the backend now; apply that on
the next frontend deploy.

## Left as-is deliberately

- **No authentication.** The old `/ask` had none either, so adding it now would
  break the live site. But `api.eliaracarflex.cfd/ask` is open to anyone who
  knows the URL and answers questions about company financials. Put Cloudflare
  Access in front of it, or have the Next.js server proxy the call and keep the
  backend private. Worth doing in the next sprint.
- **Rate limiting** is keyed on the derived session, so it's a courtesy limit,
  not a control.
- **`clarification` visuals** are not emitted: `Clarification.tsx` is wired with
  `onSelect={() => {}}`, so the option buttons do nothing. The clarifying
  question is already in `answer`, which renders correctly as text.
