# databricks-lakebase-weather-mcp
MCP server that exposes weather-forecast tools, and wire a Databricks Agent Bricks agent to use it to answer weather questions and make simple predictions/recommendations

# Weather MCP Server

FastMCP server that exposes weather forecast tools to Databricks Agent Bricks
(or any other MCP client) over HTTP. Reuses the Day 2 `weather_embeddings`
pgvector table so the agent can do both **live-forecast lookups** and
**semantic search over stored alerts** in the same conversation.

Follows the same architectural pattern as the Day 3 Alpaca MCP reference:
thin server (@mcp.tool decorators), fat broker (all HTTP + parsing logic).

## Tools

Eight tools total, grouped by purpose:

**Live forecast (Open-Meteo, no auth)**
| Tool | When Agent Bricks calls it |
|---|---|
| `resolve_location(query)` | User names a place; disambiguation needed |
| `get_current_weather(location)` | "What's the weather right now?" |
| `get_daily_forecast(location, days)` | "Weather this weekend?" |
| `get_hourly_forecast(location, hours)` | "Will it rain this afternoon?" |

**Severe weather (NWS, US-only, no auth)**
| Tool | When Agent Bricks calls it |
|---|---|
| `get_active_alerts(location)` | "Any flood warnings? Severe weather?" |

**Derived reasoning**
| Tool | When Agent Bricks calls it |
|---|---|
| `get_recommendation(location, date_offset)` | "Should I bring an umbrella? Jacket? Good day to run?" |

**Semantic search over stored data**
| Tool | When Agent Bricks calls it |
|---|---|
| `vector_search(query, limit)` | "Have there been any recent flood alerts?" |

**Identity**
| Tool | When Agent Bricks calls it |
|---|---|
| `get_current_user()` | Personalization / auth check |

Each tool has a detailed docstring — Agent Bricks reads these to decide when
to call each one. Docstrings are the most important part of an MCP server.

## Files

- `weather_mcp_server.py` — FastMCP server, all `@mcp.tool` decorators
- `weather_broker.py` — HTTP calls to Open-Meteo + NWS, plus recommendation reasoning
- `lakebase.py` — Postgres connection helper (uses `database/lakebase-url` secret)
- `app.yaml` — Databricks App deployment config
- `requirements.txt` — dependencies
- `setup_secrets.py` — one-time secret setup (only run if Lakebase secret doesn't exist yet)
- `README.md` — this file

## Data sources

All free, no API key required:

- **Open-Meteo** ([open-meteo.com](https://open-meteo.com/)) — geocoding + current, hourly, daily forecasts. Global coverage. ~10,000 calls/day for non-commercial use.
- **NWS** ([weather.gov](https://api.weather.gov/)) — active weather alerts. US only. Requires a descriptive `User-Agent` header (set via env var in `app.yaml`).
- **Lakebase (Day 2 `weather_embeddings`)** — semantic search over previously-synced NWS documents. Populated by the Day 2 sync + ingest pipeline.

## Recommendation reasoning

The `get_recommendation` tool encodes practical judgment rules in Python
rather than relying on the LLM to reason over raw numbers. Current rules:

- **Umbrella:** rain chance ≥ 70% or precip ≥ 0.25 in → yes (urgent). 40–70% → yes (precaution).
- **Jacket:** low < 40°F → warm jacket. 40–55°F → light jacket.
- **Sunscreen:** clear conditions + high ≥ 65°F → yes.
- **Outdoor activities:** unsafe if rain chance ≥ 50%, wind ≥ 25 mph, high ≥ 95°F, low ≤ 32°F, or severe weather.
- **Travel:** caution if precip ≥ 0.5 in, wind ≥ 35 mph, or hazardous conditions.

Each recommendation returns a boolean and a reasoning string the agent
can quote directly to the user.

## Running locally

```bash
pip install -r requirements.txt
python weather_mcp_server.py
```

Server listens on `http://0.0.0.0:8000` by default (honors `DATABRICKS_APP_PORT`
and `PORT` env vars).

## Deploying to Databricks

1. Push this `mcp_server/` folder to your Git repo
2. Sync into a Databricks workspace Git folder
3. Create a new Databricks App pointing at `mcp_server/`
4. Databricks reads `app.yaml`, injects the Lakebase secret, starts the server
5. Note the app URL — that's what the Agent Bricks agent will point at

First deploy takes ~2 minutes (pip installs `sentence-transformers` + downloads
the embedding model, ~200MB).

## Wiring up Agent Bricks

1. In Databricks, open **Agent Bricks** → **Create new agent**
2. Name: `Weather Assistant`
3. Add an **external MCP tool** — paste the deployed MCP server URL
4. Write the system prompt:

```
You are a helpful weather assistant with access to live weather data and a
searchable history of past weather alerts.

Available tools and when to use each:
- get_current_weather: When the user asks about weather RIGHT NOW.
- get_daily_forecast: For multi-day questions ("this weekend", "next week").
- get_hourly_forecast: For specific times within a day ("this afternoon", "tonight").
- get_active_alerts: For severe weather questions ("flood warning?", "any alerts?"). US only.
- get_recommendation: For advice questions ("should I bring an umbrella?", "do I need a jacket?", "safe to travel?"). Prefer this over reasoning over raw forecasts yourself.
- vector_search: For "have there been any X lately?" questions - searches stored NWS alerts semantically.
- resolve_location: Only if you need to disambiguate a location.

When answering, quote the specific numbers and reasoning strings from the tool
responses. Always name the location you're reporting on. If a location isn't
in the US, mention that severe weather alerts may not be available.
```

5. Save and test in the playground:
   - *"Will it rain in Chicago tomorrow?"*
   - *"Should I bring a jacket to Austin this weekend?"*
   - *"Is there a flood warning near Miami right now?"*
   - *"Have there been any recent severe weather alerts about creeks?"* ← uses `vector_search`
   - *"What should I know about the weather in Kauai on December 22nd?"* ← uses `get_recommendation`

## Observability

Every tool call is auto-logged to a `mcp_tool_traces_weather` table with
session ID, user email, parameters, result, duration, and success/failure.

Useful queries:

```sql
-- Tool usage summary for the last day
SELECT tool_name, count(*) as calls, avg(duration_ms) as avg_ms,
       sum(case when success then 0 else 1 end) as failures
FROM mcp_tool_traces_weather
WHERE created_at > NOW() - INTERVAL '1 day'
GROUP BY tool_name
ORDER BY calls DESC;

-- Recent failures
SELECT created_at, tool_name, user_email, parameters, error_message
FROM mcp_tool_traces_weather
WHERE success = false
ORDER BY created_at DESC
LIMIT 20;
```

If you want to pre-create the trace table to avoid the "must be owner of
table" error at first tool call:

```sql
CREATE TABLE IF NOT EXISTS mcp_tool_traces_weather (
    id SERIAL PRIMARY KEY,
    session_id VARCHAR(36) NOT NULL,
    tool_name VARCHAR(100) NOT NULL,
    user_email VARCHAR(255) NOT NULL,
    parameters JSONB,
    result JSONB,
    duration_ms NUMERIC(10, 2),
    success BOOLEAN NOT NULL,
    error_message TEXT,
    created_at TIMESTAMP DEFAULT NOW()
);
```

## Known limitations

- **NWS is US-only.** International severe-weather alerts would need a different provider.
- **`vector_search` requires the Day 2 pipeline to be running.** If `weather_embeddings` is empty, results will be empty.
- **Recommendation rules are hardcoded.** Encoded as Python thresholds — easy to tune, but no ML judgment.
- **No caching.** Every tool call hits the upstream API.
- **Location resolution is basic.** Ambiguous queries always pick the top Open-Meteo geocoding match.
