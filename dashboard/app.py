"""
Weather MCP dashboard: a small FastAPI app to WATCH what the Agent Bricks
agent has been asking weather_mcp_server.py, and to let a human run the
same lookups by hand to sanity-check the agent's answers. It never places
anything and never calls the MCP tools directly; it calls the same
weather_client.py / recommendations.py functions the tools call, so the
numbers a human sees here are computed the same way.

Deploy this as its OWN Databricks App (separate from weather_mcp_server.py) -
one app serves MCP tool calls, the other serves the human-facing UI.

Run locally:
    python app.py
"""

import logging
import os

from fastapi import FastAPI, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

import query_log
import recommendations
import weather_client
from weather_client import (
    InvalidRequestError,
    LocationNotFoundError,
    UpstreamAPIError,
    WeatherClientError,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("weather-dashboard")

app = FastAPI(title="Weather MCP Dashboard")
# Absolute path: Databricks Apps and `python app.py` from the repo root can
# both launch this with a different working directory than dashboard/.
_TEMPLATES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates")
templates = Jinja2Templates(directory=_TEMPLATES_DIR)


@app.exception_handler(WeatherClientError)
async def weather_client_error_handler(request: Request, exc: WeatherClientError) -> JSONResponse:
    """Map weather_client's typed errors to HTTP status codes, JSON only, no stack trace."""
    if isinstance(exc, LocationNotFoundError):
        status_code = 404
    elif isinstance(exc, InvalidRequestError):
        status_code = 400
    elif isinstance(exc, UpstreamAPIError):
        status_code = 502
    else:
        status_code = 500
    return JSONResponse(status_code=status_code, content={"message": str(exc)})


@app.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    """Dashboard page: location search, forecast strip, and the recent agent queries table."""
    return templates.TemplateResponse(request, "index.html")


@app.get("/healthz")
async def healthz() -> dict:
    return {"status": "ok"}


@app.get("/api/recent")
def api_recent(limit: int = Query(default=50, ge=1, le=200)) -> dict:
    """Recent MCP tool calls logged to Lakebase, most recent first, plus whether logging is on at all."""
    return {"logging_enabled": query_log.is_enabled(), "queries": query_log.fetch_recent(limit)}


@app.get("/api/current")
def api_current(location: str = Query(..., min_length=1)) -> dict:
    """Live current conditions for a location, the same call get_current_weather makes."""
    deadline = weather_client.new_deadline()
    place = weather_client.resolve_location(location, deadline=deadline)
    return weather_client.current_conditions(place, deadline=deadline)


@app.get("/api/forecast")
def api_forecast(location: str = Query(..., min_length=1), days: int = Query(default=5)) -> dict:
    """
    Live daily forecast for a location, the same call get_forecast makes.

    days is not range-checked here on purpose: weather_client.daily_forecast
    raises InvalidRequestError for an out-of-range value, and the exception
    handler above turns that into a 400 with a message naming the valid
    range, instead of FastAPI's generic 422.
    """
    deadline = weather_client.new_deadline()
    place = weather_client.resolve_location(location, deadline=deadline)
    return weather_client.daily_forecast(place, days, deadline=deadline)


@app.get("/api/advice")
def api_advice(location: str = Query(..., min_length=1), date: str | None = Query(default=None)) -> dict:
    """
    Umbrella advice plus a travel score for a location and day, the same
    combination get_umbrella_advice and get_travel_recommendation make, so
    a human can compare their own read of the forecast against the agent's.
    """
    deadline = weather_client.new_deadline()
    place = weather_client.resolve_location(location, deadline=deadline)
    day, resolved_date = weather_client.forecast_day_for_date(place, date, deadline=deadline)

    alerts_status = "ok"
    alerts = []
    try:
        alerts_result = weather_client.active_alerts(place, deadline=deadline)
        # Same reasoning as get_travel_recommendation: an alert that has
        # already ended before this day starts should not dock its score.
        alerts = weather_client.filter_alerts_for_date(alerts_result["alerts"], resolved_date)
    except UpstreamAPIError:
        logger.warning("Alerts lookup failed for %s; scoring without them.", place.get("label"))
        alerts_status = "unavailable"

    return {
        "location": place,
        "date": resolved_date,
        "forecast_day": day,
        "advice": recommendations.umbrella_advice(day),
        "recommendation": recommendations.travel_score(day, alerts),
        "alerts": alerts,
        "alerts_status": alerts_status,
    }


if __name__ == "__main__":
    import uvicorn

    port = int(os.getenv("DATABRICKS_APP_PORT", os.getenv("PORT", 8001)))
    uvicorn.run(app, host="0.0.0.0", port=port)
