# Weather Intelligence

An end-to-end AI-powered weather system on Databricks:

- **Day 2** — retrieval-augmented (RAG) semantic search over unstructured weather narratives from the National Weather Service
- **Day 3** — a FastMCP server exposing live forecast tools + the Day 2 RAG data, wired to a Databricks Agent Bricks agent

Ask *"flash flood risk this weekend?"* or *"should I bring an umbrella to Chicago tomorrow?"* and the agent chooses the right tool (live forecast vs. historical alert search vs. derived recommendation), calls it, and answers in natural language.

---

## Update — Agent configuration documentation added

Following initial grading feedback, the `agent/` folder now contains the
verbatim system prompt configured in Agent Bricks, along with screenshots
verifying agent behavior against each guardrail (rubric items 4.2, 4.3, 4.4).

---

## What this project does

Traditional keyword search can't tell that *"heavy rainfall causing rapid rises on creeks"* is relevant to a query about *"flood risk near rivers."* This project solves that by embedding weather narratives into vector space, then finding matches by cosine similarity. Then it layers an MCP server + AI agent on top so a chat agent can autonomously combine live-forecast tools with semantic historical search.

End-to-end, the pipeline:

1. **Harvests** unstructured alerts and forecasts from the National Weather Service
2. **Chunks and embeds** the narrative text using `sentence-transformers/all-MiniLM-L6-v2` (384-dim)
3. **Stores** the vectors in Lakebase (Postgres + pgvector) with an HNSW cosine-similarity index
4. **Serves** a Flask REST API (Day 2) and an MCP server (Day 3) that share the same data
5. **Powers** an Agent Bricks agent that reasons over both live forecasts and historical alerts

---

## Tech stack

- **Backend** — Python, Flask, FastMCP
- **Database** — PostgreSQL (Databricks Lakebase) with pgvector extension
- **Embeddings** — `sentence-transformers/all-MiniLM-L6-v2` (384-dim)
- **Live data** — Open-Meteo API (global forecasts) + National Weather Service API (US alerts)
- **Platform** — Databricks Apps (both Flask and MCP server deployed as separate apps)
- **AI** — Databricks Agent Bricks with external MCP tool

---

## Repo structure

```
.
├── README.md                           # this file
├── .gitignore
├── .env.example
├── requirements.txt
├── setup_secrets.py
├── databricks.yml
│
├── app.py                              # Day 2: Flask app (/weather/sync, /weather/search)
├── app.yaml                            # Day 2: Databricks App config
├── lakebase.py                         # Postgres connection helper
├── weather_client.py                   # NWS API client
├── templates/
│   └── index.html                      # Day 2: search UI
├── sql/
│   ├── 01_setup_weather_documents_table.sql
│   ├── 02_setup_weather_embeddings_table.sql
│   └── README.md
├── notebooks/
│   └── ingest_weather_embeddings.py    # Day 2: batch embedding job
├── resources/
│   └── ingest_weather_embeddings_job.yml
│
├── mcp_server/                         # Day 3: FastMCP server + broker
│   ├── weather_mcp_server.py           # 8 tools with auto-tracing
│   ├── weather_broker.py               # Open-Meteo + NWS + recommendation logic
│   ├── lakebase.py                     # Postgres helper (lazy WorkspaceClient)
│   ├── app.yaml                        # Databricks App config
│   ├── requirements.txt
│   ├── setup_secrets.py
│   └── README.md
│
├── agent/                              # Day 3: Agent Bricks configuration
│   ├── README.md                       # Full agent docs + rubric mapping
│   ├── system_prompt.md                # Verbatim configured system prompt
│   └── screenshots/                    # Config + test conversation screenshots
│
└── docs/
    └── DEPLOYMENT_ISSUES.md            # 5 real issues + fixes from deployment
```

---

## Day 2 — Weather RAG Pipeline

A Flask app that ingests NWS narratives, embeds them, and serves semantic search over them.

**Endpoints:**
- `POST /weather/sync` — fetch alerts + forecasts from NWS for a list of locations, upsert into `weather_documents`
- `POST /weather/search` — cosine similarity search over `weather_embeddings`, returns top-K ranked chunks

**Tables in Lakebase:**
- `weather_documents` — raw NWS documents (one row per alert or forecast period)
- `weather_embeddings` — chunked narrative embeddings (VECTOR(384), HNSW indexed for cosine distance)

Full setup and run instructions: see `mcp_server/../` deployment steps and `sql/README.md`.

---

## Day 3 — MCP Server + Agent Bricks

A FastMCP server exposing 8 tools to a Databricks Agent Bricks agent. Reuses the Day 2 `weather_embeddings` table as one of its tools (`vector_search`), and adds live-forecast tools from Open-Meteo + severe weather alerts from NWS.

**Tools exposed (8 total):**

| Tool | Data source | When Agent Bricks calls it |
|---|---|---|
| `resolve_location(query)` | Open-Meteo | Location disambiguation |
| `get_current_weather(location)` | Open-Meteo | "Right now" questions |
| `get_daily_forecast(location, days)` | Open-Meteo | Multi-day questions |
| `get_hourly_forecast(location, hours)` | Open-Meteo | Within-day questions |
| `get_active_alerts(location)` | NWS (US-only) | Severe weather questions |
| `get_recommendation(location, date_offset)` | Derived logic | Advice questions (umbrella, jacket, travel) |
| `vector_search(query, limit)` | Lakebase pgvector | "Have there been any X lately?" (historical) |
| `get_current_user()` | Request context | Identity/personalization |

**Deployed as its own Databricks App**, separate from the Day 2 Flask app. Agent Bricks connects to it as an external MCP tool.

Full details: `mcp_server/README.md`.

---

## Agent Bricks Configuration

The `agent/` folder documents the live agent configuration:

- **`agent/system_prompt.md`** — verbatim system prompt currently configured in Agent Bricks
- **`agent/README.md`** — full agent documentation with rubric mapping
- **`agent/screenshots/`** — configuration screenshot + test conversations demonstrating each guardrail

The system prompt includes:
- Explicit tool selection rules for each user intent
- 6 guardrails (no fabrication, no medical advice, no safety guarantees, location clarification, non-US caveats, tool authority)
- Response format guidelines

Behavior verification screenshots in `agent/screenshots/` demonstrate:
- Location clarification (agent asks "which Springfield?" instead of guessing)
- Non-US caveat (agent notes NWS is US-only for Tokyo queries)
- No fabrication (agent refuses to invent data for unknown locations)

---

## Observability

Every MCP tool call is auto-logged to `mcp_tool_traces_weather` in Lakebase:

```sql
SELECT tool_name, count(*) as calls, avg(duration_ms) as avg_ms
FROM mcp_tool_traces_weather
WHERE created_at > NOW() - INTERVAL '1 day'
GROUP BY tool_name
ORDER BY calls DESC;
```

Tracked fields: session ID, user email (from `X-Forwarded-User`), parameters, result, duration, success/failure, error message.

---

## Getting started

### Prerequisites

- A Databricks workspace with Apps enabled
- A Lakebase (Databricks-managed Postgres) instance with `pgvector` enabled
- The Lakebase connection URL stored in a Databricks secret at `database/lakebase-url`

### One-time setup

```bash
# Store the Lakebase URL as a Databricks secret
python setup_secrets.py

# Create the schema
psql "$LAKEBASE_URL" -f sql/01_setup_weather_documents_table.sql
psql "$LAKEBASE_URL" -f sql/02_setup_weather_embeddings_table.sql
```

### Deploy the Day 2 Flask app

1. Push to GitHub, sync to a Databricks git folder
2. Create a Databricks App pointing at the repo root
3. App reads `app.yaml`, connects to Lakebase, starts serving `/weather/sync` and `/weather/search`

### Deploy the Day 3 MCP server

1. Same git folder
2. Create a *second* Databricks App pointing at `mcp_server/` (the subdirectory, not the repo root)
3. Note the deployed URL

### Wire up Agent Bricks

1. Open Agent Bricks → Create new agent
2. Add the MCP server URL as an external MCP tool
3. Paste the system prompt from `agent/system_prompt.md`
4. Test in the playground

Full instructions: `mcp_server/README.md` and `agent/README.md`.

---

## Deployment lessons learned

`docs/DEPLOYMENT_ISSUES.md` documents 5 real production issues hit during deployment with root causes and fixes:

1. `must be owner of table` error from `CREATE TABLE IF NOT EXISTS` in app code
2. Same error on the auto-created `mcp_tool_traces_weather` table
3. App deployed from wrong source (hello-world scaffold instead of actual code)
4. `OAuth Token not supported for current auth type PAT` from eager `WorkspaceClient()` initialization
5. Deploying from parent git folder vs. `mcp_server/` subdirectory

Includes a 7-item deployment checklist derived from these fixes.

---

## Known limitations & future work

- **NWS is US-only.** For international severe weather alerts, a different provider would be needed.
- **`vector_search` requires the Day 2 pipeline to be running.** If `weather_embeddings` is empty, results will be empty.
- **Location resolution is basic.** Ambiguous queries either ask the user or pick the top Open-Meteo geocoding match.
- **Recommendation rules are hardcoded thresholds.** Easy to tune, but no ML judgment.
- **No caching.** Every MCP tool call hits the upstream API.
