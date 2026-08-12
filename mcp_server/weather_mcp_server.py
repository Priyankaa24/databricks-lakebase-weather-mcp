"""
Weather MCP server.

Exposes weather forecast tools over MCP (Model Context Protocol) so a
Databricks Agent Bricks agent can call them like any other tool:

Live weather (Open-Meteo, no auth):
    - resolve_location(query) - Resolve a place name to lat/lon
    - get_current_weather(location) - Current conditions
    - get_daily_forecast(location, days) - Multi-day forecast
    - get_hourly_forecast(location, hours) - Hourly forecast

Severe weather (NWS, US-only, no auth):
    - get_active_alerts(location) - Active NWS alerts

Semantic search over stored weather documents (Day 2 pgvector layer):
    - vector_search(query, limit) - Cosine-similarity search over
      weather_documents/weather_embeddings

User identity:
    - get_current_user() - Get authenticated user info

Tracing & Monitoring:
    All tool calls are automatically traced to a Lakebase table
    (mcp_tool_traces) with session ID, user email, parameters, results,
    duration, and success status.

Deploy this as its own Databricks App (see app.yaml), separate from the
Day 2 Flask app.

Run locally:
    python weather_mcp_server.py
"""

import inspect
import json
import logging
import os
import time
import uuid
from contextvars import ContextVar
from functools import wraps

from fastmcp import FastMCP
from sentence_transformers import SentenceTransformer
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

import lakebase
import weather_broker

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("weather-mcp-server")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

WEATHER_DOCS_TABLE = os.environ.get("WEATHER_DOCS_TABLE", "weather_documents")
WEATHER_EMBEDDINGS_TABLE = os.environ.get("WEATHER_EMBEDDINGS_TABLE", "weather_embeddings")
EMBEDDING_MODEL = os.environ.get("EMBEDDING_MODEL", "sentence-transformers/all-MiniLM-L6-v2")

# ---------------------------------------------------------------------------
# Embedding model (lazy-loaded on first vector_search call)
# ---------------------------------------------------------------------------

_embedding_model = None


def get_embedding_model():
    """Lazy-load the embedding model (expensive; only on first use)."""
    global _embedding_model
    if _embedding_model is None:
        logger.info(f"Loading embedding model: {EMBEDDING_MODEL}")
        _embedding_model = SentenceTransformer(EMBEDDING_MODEL)
    return _embedding_model


# ---------------------------------------------------------------------------
# Request context (captured from HTTP headers by middleware)
# ---------------------------------------------------------------------------

_request_context: ContextVar[dict] = ContextVar('request_context', default={})


def _get_end_user_email() -> str:
    """Get the end user's email from request headers, else the service principal."""
    headers = _request_context.get()
    forwarded_user = headers.get('x-forwarded-user')
    if forwarded_user:
        return forwarded_user

    from databricks.sdk import WorkspaceClient
    w = WorkspaceClient()
    return w.current_user.me().user_name or 'unknown@user'


# ---------------------------------------------------------------------------
# Session ID for grouping tool calls from this server instance
# ---------------------------------------------------------------------------

SESSION_ID = str(uuid.uuid4())
logger.info(f"MCP Server Session ID: {SESSION_ID}")


# ---------------------------------------------------------------------------
# Trace logging to Lakebase
# ---------------------------------------------------------------------------

# Flag so we only try to create the table once per server lifetime.
# If it fails (e.g. ownership error), we assume the table already exists
# and skip the DDL on subsequent calls.
_traces_table_checked = False


def _ensure_traces_table():
    """Idempotently ensure the traces table exists. Runs once per process."""
    global _traces_table_checked
    if _traces_table_checked:
        return
    _traces_table_checked = True

    try:
        lakebase.run_write(
            """
            CREATE TABLE IF NOT EXISTS mcp_tool_traces (
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
            )
            """,
            (),
        )
    except Exception as e:
        # Common cases: "must be owner of table" if it already exists but
        # was created by a different role. We proceed and rely on INSERTs
        # succeeding (they only need INSERT privilege, not ownership).
        logger.warning(f"Could not ensure mcp_tool_traces table (may already exist): {e}")


def _log_tool_call_to_lakebase(
    tool_name: str,
    parameters: dict,
    result: dict,
    duration_ms: float,
    success: bool,
    error_message: str = None,
):
    """Log an MCP tool call to Lakebase for monitoring/analytics."""
    try:
        _ensure_traces_table()
        user_email = _get_end_user_email()
        lakebase.run_write(
            """
            INSERT INTO mcp_tool_traces
                (session_id, tool_name, user_email, parameters, result, duration_ms, success, error_message)
            VALUES
                (%s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                SESSION_ID,
                tool_name,
                user_email,
                json.dumps(parameters, default=str),
                json.dumps(result, default=str) if result else None,
                duration_ms,
                success,
                error_message,
            ),
        )
    except Exception as e:
        logger.warning(f"Failed to log tool trace for {tool_name}: {e}")


def traced_tool(func):
    """
    Decorator that wraps MCP tools to add automatic tracing to Lakebase.
    Captures tool name, input params, return value, duration, success, user.
    """
    @wraps(func)
    def wrapper(*args, **kwargs):
        start_time = time.time()
        tool_name = func.__name__

        sig = inspect.signature(func)
        bound_args = sig.bind(*args, **kwargs)
        bound_args.apply_defaults()
        parameters = dict(bound_args.arguments)

        result = None
        success = True
        error_message = None

        try:
            result = func(*args, **kwargs)
            return result
        except Exception as e:
            success = False
            error_message = str(e)
            result = {"status": "error", "message": str(e)}
            raise
        finally:
            duration_ms = (time.time() - start_time) * 1000
            try:
                _log_tool_call_to_lakebase(
                    tool_name=tool_name,
                    parameters=parameters,
                    result=result,
                    duration_ms=duration_ms,
                    success=success,
                    error_message=error_message,
                )
            except Exception:
                pass

    return wrapper


# ---------------------------------------------------------------------------
# FastMCP setup
# ---------------------------------------------------------------------------

mcp = FastMCP("weather")


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Capture HTTP headers containing end-user identity into a ContextVar."""
    async def dispatch(self, request: Request, call_next):
        headers = {
            'x-forwarded-user': request.headers.get('x-forwarded-user'),
            'x-forwarded-email': request.headers.get('x-forwarded-email'),
        }
        _request_context.set(headers)
        response = await call_next(request)
        return response


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

@mcp.tool
@traced_tool
def resolve_location(query: str) -> dict:
    """
    Resolve a human-readable location name to latitude/longitude via Open-Meteo.

    Use this when the user names a place and you need coordinates or need
    to verify it exists. Other weather tools accept location names directly,
    so you usually don't need to call this first - it's useful for
    disambiguation or when the user asks 'where is X?'.

    Args:
        query: A place name, e.g. "Chicago", "Paris", "Kauai", "10001".

    Returns:
        A dict with status ("success" or "error"), and on success the
        resolved name, country, state/admin1, latitude, longitude, timezone.
    """
    try:
        result = weather_broker.geocode(query)
        return {"status": "success", **result}
    except Exception as e:
        logger.exception(f"resolve_location failed for {query!r}")
        return {"status": "error", "message": f"Failed to resolve location: {str(e)}"}


@mcp.tool
@traced_tool
def get_current_weather(location: str) -> dict:
    """
    Get the current weather conditions at a location (Open-Meteo).

    Use this when the user asks about the weather RIGHT NOW - not later
    today or tomorrow. Returns temperature (F), feels-like temperature,
    humidity, precipitation, wind, cloud cover, and text conditions.

    Args:
        location: A place name, e.g. "Austin, TX" or "Tokyo".

    Returns:
        A dict with status, and on success: observed_at, temperature_f,
        feels_like_f, humidity_pct, precipitation_in, wind_mph,
        wind_direction_deg, cloud_cover_pct, conditions text.
    """
    try:
        result = weather_broker.get_current(location)
        return {"status": "success", **result}
    except Exception as e:
        logger.exception(f"get_current_weather failed for {location!r}")
        return {"status": "error", "message": f"Failed to fetch weather: {str(e)}"}


@mcp.tool
@traced_tool
def get_daily_forecast(location: str, days: int = 3) -> dict:
    """
    Get a daily weather forecast for a location for the next N days (1-7).

    Use this when the user asks about multi-day weather - 'this weekend',
    'next week', 'tomorrow through Friday'. Returns one entry per day with
    high/low temps, precipitation chance, max wind, sunrise/sunset, and
    conditions text.

    Args:
        location: A place name, e.g. "Chicago, IL".
        days: Number of days to forecast, from 1 to 7 (default 3).

    Returns:
        A dict with status, and on success a 'days' list with date, high_f,
        low_f, precipitation_chance_pct, precipitation_in, max_wind_mph,
        conditions, sunrise, sunset per day.
    """
    try:
        result = weather_broker.get_daily_forecast(location, days)
        return {"status": "success", **result}
    except Exception as e:
        logger.exception(f"get_daily_forecast failed for {location!r}")
        return {"status": "error", "message": f"Failed to fetch forecast: {str(e)}"}


@mcp.tool
@traced_tool
def get_hourly_forecast(location: str, hours: int = 24) -> dict:
    """
    Get an hourly weather forecast for a location for the next N hours (1-72).

    Use this when the user asks about specific times within a day -
    'will it rain this afternoon?', 'is it clear tonight?', 'when should
    I go running?'. Returns one entry per hour.

    Args:
        location: A place name, e.g. "Seattle".
        hours: Number of hours to forecast, from 1 to 72 (default 24).

    Returns:
        A dict with status, and on success an 'hours' list with time,
        temperature_f, precipitation_chance_pct, precipitation_in,
        wind_mph, and conditions per hour.
    """
    try:
        result = weather_broker.get_hourly_forecast(location, hours)
        return {"status": "success", **result}
    except Exception as e:
        logger.exception(f"get_hourly_forecast failed for {location!r}")
        return {"status": "error", "message": f"Failed to fetch hourly forecast: {str(e)}"}


@mcp.tool
@traced_tool
def get_active_alerts(location: str) -> dict:
    """
    Get active National Weather Service alerts for a US location.

    Use this when the user asks about severe weather warnings - 'is there
    a flood warning?', 'any tornado watches?', 'is it safe to travel?'.
    Returns flash flood warnings, tornado watches, winter storm warnings,
    heat advisories, etc.

    Note: NWS covers US locations only. For non-US locations, returns an
    empty alerts list with a note.

    Args:
        location: A place name, e.g. "Miami, FL".

    Returns:
        A dict with status, and on success an 'alerts' list with event,
        headline, severity, urgency, certainty, sent, expires, description,
        and safety instruction per alert.
    """
    try:
        result = weather_broker.get_active_alerts(location)
        return {"status": "success", **result}
    except Exception as e:
        logger.exception(f"get_active_alerts failed for {location!r}")
        return {"status": "error", "message": f"Failed to fetch alerts: {str(e)}"}


@mcp.tool
@traced_tool
def vector_search(query: str, limit: int = 5) -> dict:
    """
    Semantic search over stored weather documents (alerts + forecasts) using
    pgvector cosine similarity.

    Use this when the user asks about weather patterns, historical alerts,
    or concepts rather than a specific live forecast - e.g. 'any flooding
    concerns near rivers?', 'reports of heavy snowfall lately?', 'what
    severe weather has been active?'.

    This is DIFFERENT from get_active_alerts (which fetches live NWS
    alerts for one point right now). This searches the internal document
    store populated by prior /weather/sync runs from the Day 2 Flask app,
    so it can find semantically-related content across many locations and
    time periods.

    Args:
        query: A natural-language search query.
        limit: Maximum results to return, from 1 to 20 (default 5).

    Returns:
        A dict with status, and on success: query, model, and a 'results'
        list with location, headline, source_type, chunk_text, issued_at,
        similarity score per row.
    """
    if not query or not query.strip():
        return {"status": "error", "message": "Query text is required"}

    limit = max(1, min(20, int(limit)))

    try:
        model = get_embedding_model()
        query_vec = model.encode(query).tolist()
        query_vec_str = "[" + ",".join(str(x) for x in query_vec) + "]"

        results = lakebase.run_query(
            f"""
            SELECT d.id,
                   d.location,
                   d.source_type,
                   d.headline,
                   e.chunk_text,
                   d.issued_at,
                   1 - (e.embedding <=> %s::vector) AS similarity
            FROM {WEATHER_EMBEDDINGS_TABLE} e
            JOIN {WEATHER_DOCS_TABLE} d ON d.id = e.document_id
            ORDER BY e.embedding <=> %s::vector
            LIMIT %s
            """,
            (query_vec_str, query_vec_str, limit),
        )

        return {
            "status": "success",
            "query": query,
            "model": EMBEDDING_MODEL,
            "results": results,
        }

    except Exception as e:
        logger.exception("Vector search failed")
        return {"status": "error", "message": f"Vector search failed: {str(e)}"}


@mcp.tool
@traced_tool
def get_current_user() -> dict:
    """
    Get information about the currently authenticated end user accessing
    the MCP server.

    When running as a Databricks App, returns the actual end user making
    the request (from X-Forwarded-User header), not the service principal
    running the app.

    Returns:
        A dict with status, and on success: user_name (email), and source
        ("request_header" or "service_principal").
    """
    try:
        headers = _request_context.get()
        forwarded_user = headers.get('x-forwarded-user')
        forwarded_email = headers.get('x-forwarded-email')

        if forwarded_user:
            return {
                "status": "success",
                "user_name": forwarded_user,
                "forwarded_email": forwarded_email,
                "source": "request_header",
            }

        from databricks.sdk import WorkspaceClient
        w = WorkspaceClient()
        user = w.current_user.me()
        return {
            "status": "success",
            "user_name": user.user_name,
            "display_name": user.display_name,
            "active": user.active,
            "source": "service_principal",
        }
    except Exception as e:
        logger.exception("Failed to get current user")
        return {"status": "error", "message": f"Failed to get current user: {str(e)}"}


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    # Add middleware to capture request headers for end-user identity.
    if hasattr(mcp, 'app') and mcp.app is not None:
        mcp.app.add_middleware(RequestContextMiddleware)

    port = int(os.getenv("DATABRICKS_APP_PORT", os.getenv("PORT", 8000)))
    mcp.run(transport="http", host="0.0.0.0", port=port)
