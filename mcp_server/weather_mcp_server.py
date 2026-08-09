"""
Weather MCP server.

Exposes weather tools over MCP (Model Context Protocol) so a Databricks
Agent Bricks agent can call them like any other tool:
    - resolve_location(query)
    - get_current_weather(location)
    - get_forecast(location, days)
    - get_umbrella_advice(location, date)
    - get_travel_recommendation(location, date)
    - get_severe_weather_alerts(location)
    - compare_locations(locations, date)
    - get_historical_weather(location, start_date, end_date)

These tools are backed by Open-Meteo (geocoding, forecast, historical
archive) and the US National Weather Service (active alerts), both keyless
- see weather_client.py for the HTTP/parsing layer and recommendations.py
for the derived-judgment logic (umbrella advice, travel scoring, packing
lists). Every tool here is a thin wrapper: resolve a location, call one or
two weather_client/recommendations functions, shape the result, log it,
and return - no HTTP calls and no derived-judgment math live in this file.

Deploy this as its own Databricks App (same app.yaml + FastMCP entrypoint
pattern documented at
https://docs.databricks.com/aws/en/agents/mcp-tools/custom-mcp), separate
from the dashboard app, so an Agent Bricks agent (or any MCP client) can
register its URL as an external MCP server.

Run locally:
    python weather_mcp_server.py
"""

import logging
import os
import time
from contextvars import ContextVar

import uvicorn
from fastmcp import FastMCP
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

import query_log
import recommendations
import weather_client
from weather_client import (
    InvalidRequestError,
    LocationNotFoundError,
    UpstreamAPIError,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("weather-mcp-server")

MAX_COMPARE_LOCATIONS = 5

mcp = FastMCP("weather-forecast")


@mcp.custom_route("/healthz", methods=["GET"])
async def healthz(request: Request) -> JSONResponse:
    """Plain liveness check; Databricks Apps and any load balancer in front of this app can poll it."""
    return JSONResponse({"status": "ok"})


# Captures x-forwarded-user / x-forwarded-email, the identity headers
# Databricks Apps injects for the actual end user (as opposed to the
# service principal running the app), so query_log can attribute rows to
# a real user. Populated by ForwardedIdentityMiddleware below.
_request_context: ContextVar[dict] = ContextVar("request_context", default={})


class ForwardedIdentityMiddleware(BaseHTTPMiddleware):
    """
    Starlette ASGI middleware that captures end-user identity headers into
    a ContextVar.

    Named ForwardedIdentityMiddleware, not RequestContextMiddleware: the
    old name collided by name (not by identity) with one of fastmcp's own
    internal middlewares, and the two rendered identically in
    app.user_middleware and in tracebacks, which made debugging either one
    needlessly confusing.
    """

    async def dispatch(self, request: Request, call_next):
        _request_context.set({
            "x-forwarded-user": request.headers.get("x-forwarded-user"),
            "x-forwarded-email": request.headers.get("x-forwarded-email"),
        })
        return await call_next(request)


def _requested_by() -> str | None:
    headers = _request_context.get()
    return headers.get("x-forwarded-email") or headers.get("x-forwarded-user")


def _log_safely(**kwargs) -> None:
    """Call query_log.record, and if it somehow still raises, swallow it - logging must never fail a tool."""
    try:
        query_log.record(requested_by=_requested_by(), **kwargs)
    except Exception:
        logger.exception("query_log.record raised despite its own error handling; ignoring.")


def _tool_call(tool_name: str, location_query: str | None, fn):
    """
    Run a tool body, convert weather_client exceptions into the tool's
    error-result shape, and log the outcome. `fn` returns
    (payload: dict, resolved: dict | None, verdict: str | None, summary: str).

    Any WeatherClientError can carry a `context` dict (see weather_client.py);
    its keys are merged into the error result on top of status/message, so a
    tool can attach extra structured detail (e.g. compare_locations' `failed`
    list) without each error arm needing its own bespoke shape.
    """
    started = time.monotonic()
    resolved = None
    try:
        payload, resolved, verdict, summary = fn()
        status = "ok"
        result = {"status": "ok", **payload}
    except LocationNotFoundError as exc:
        status = "not_found"
        verdict = None
        summary = str(exc)
        result = {
            "status": "not_found",
            "message": str(exc),
            "hint": "Try a more specific query, e.g. \"City, State\" or \"City, Country\", or pass a \"lat,lon\" pair.",
            **exc.context,
        }
    except InvalidRequestError as exc:
        status = "invalid_request"
        verdict = None
        summary = str(exc)
        result = {"status": "invalid_request", "message": str(exc), **exc.context}
    except UpstreamAPIError as exc:
        status = "upstream_error"
        verdict = None
        summary = str(exc)
        result = {"status": "upstream_error", "message": str(exc), **exc.context}
    except Exception as exc:
        logger.exception("Unhandled error in tool %s", tool_name)
        status = "error"
        verdict = None
        summary = str(exc)
        result = {"status": "error", "message": str(exc)}

    duration_ms = int((time.monotonic() - started) * 1000)
    _log_safely(
        tool_name=tool_name,
        location_query=location_query,
        resolved=resolved,
        status=status,
        verdict=verdict,
        summary=summary,
        duration_ms=duration_ms,
    )
    return result


def _resolve_or_raise(location: str, *, deadline: float | None = None) -> dict:
    return weather_client.resolve_location(location, deadline=deadline)


@mcp.tool
def resolve_location(query: str) -> dict:
    """
    Resolve a free-text place name or "lat,lon" pair into coordinates.

    Args:
        query: A place name ("Chicago", "Austin, TX", "Porto Alegre, Brazil")
            or a bare "lat,lon" pair (e.g. "41.88,-87.63").

    Returns:
        A dict with status, and on success the resolved location (name,
        admin1, country, country_code, latitude, longitude, timezone,
        label, source) plus alternatives (up to 4 other candidate labels,
        so the agent can ask the user to disambiguate).
    """
    def body():
        place = _resolve_or_raise(query)
        summary = f"Resolved {query!r} to {place['label'] or place['name']}."
        return place, place, None, summary

    return _tool_call("resolve_location", query, body)


@mcp.tool
def get_current_weather(location: str) -> dict:
    """
    Get current temperature, feels-like, humidity, wind, precipitation, and conditions.

    Args:
        location: A place name or "lat,lon" pair.

    Returns:
        A dict with status and, on success, the current-conditions payload
        (temperature_c/f, feels_like_c/f, humidity_pct, precipitation_mm/in,
        wind_speed_kmh/mph, wind_direction_deg/compass, pressure_hpa,
        is_day, weather_code, conditions).
    """
    def body():
        # This tool makes two upstream calls (resolve, then current
        # conditions); share one time budget across both so a slow
        # geocoder does not leave the second call a fresh 60s on top.
        deadline = weather_client.new_deadline()
        place = _resolve_or_raise(location, deadline=deadline)
        current = weather_client.current_conditions(place, deadline=deadline)
        summary = f"{place['label'] or place['name']}: {current['conditions']}, {current['temperature_c']} C."
        return current, place, None, summary

    return _tool_call("get_current_weather", location, body)


@mcp.tool
def get_forecast(location: str, days: int = 5) -> dict:
    """
    Get a daily forecast for the next N days.

    Args:
        location: A place name or "lat,lon" pair.
        days: Number of days to forecast, 1-16 (default 5).

    Returns:
        A dict with status and, on success, location, timezone, and a
        `days` list of daily forecast dicts (temp_high/low_c/f, conditions,
        precipitation, wind, uv_index_max, sunrise, sunset).
    """
    def body():
        deadline = weather_client.new_deadline()
        place = _resolve_or_raise(location, deadline=deadline)
        forecast = weather_client.daily_forecast(place, days, deadline=deadline)
        summary = f"{place['label'] or place['name']}: {len(forecast['days'])}-day forecast."
        return forecast, place, None, summary

    return _tool_call("get_forecast", location, body)


@mcp.tool
def get_umbrella_advice(location: str, date: str | None = None) -> dict:
    """
    Decide whether to bring an umbrella for a given day, with a plain-
    English reason naming the numbers behind the call.

    Args:
        location: A place name or "lat,lon" pair.
        date: YYYY-MM-DD, within the next 16 days. None means today.

    Returns:
        A dict with status and, on success, `advice` (verdict, confidence,
        reason, rule_fired, wind_warning, inputs_used) plus `forecast_day`,
        the raw forecast day the advice reasoned over, so the agent can
        quote real numbers back to the user.
    """
    def body():
        deadline = weather_client.new_deadline()
        place = _resolve_or_raise(location, deadline=deadline)
        day, resolved_date = weather_client.forecast_day_for_date(place, date, deadline=deadline)
        advice = recommendations.umbrella_advice(day)
        summary = f"{place['label'] or place['name']} on {resolved_date}: umbrella {advice['verdict']}."
        payload = {"location": place, "date": resolved_date, "advice": advice, "forecast_day": day}
        return payload, place, advice["verdict"], summary

    return _tool_call("get_umbrella_advice", location, body)


@mcp.tool
def get_travel_recommendation(location: str, date: str | None = None) -> dict:
    """
    Get a composite 0-100 travel score for a given day, with a factor
    breakdown, a packing list, and any active severe weather alerts folded
    into the score.

    Args:
        location: A place name or "lat,lon" pair.
        date: YYYY-MM-DD, within the next 16 days. None means today.

    Returns:
        A dict with status and, on success, `recommendation` (score, band,
        factors, headline, packing_list, alerts_considered) plus
        `forecast_day`, `alerts` (the active alerts that were actually
        folded into the score, so the agent can state each one's event,
        severity, area, and instruction instead of just a count), and
        `alerts_status` ("ok" or "unavailable" if the alerts call itself
        failed; the score still gets returned in that case).
    """
    def body():
        # Up to 3 upstream calls (resolve, forecast, alerts); share one
        # time budget across all of them instead of each getting its own
        # fresh 60s, which could otherwise chain to several minutes.
        deadline = weather_client.new_deadline()
        place = _resolve_or_raise(location, deadline=deadline)
        day, resolved_date = weather_client.forecast_day_for_date(place, date, deadline=deadline)

        alerts_status = "ok"
        alerts = []
        try:
            alerts_result = weather_client.active_alerts(place, deadline=deadline)
            # An alert that ends or expires before this day starts is not
            # this day's problem; do not let it dock the score.
            alerts = weather_client.filter_alerts_for_date(alerts_result["alerts"], resolved_date)
        except UpstreamAPIError:
            logger.warning("Alerts lookup failed for %s; scoring without them.", place.get("label"))
            alerts_status = "unavailable"

        recommendation = recommendations.travel_score(day, alerts)
        summary = f"{place['label'] or place['name']} on {resolved_date}: travel score {recommendation['score']} ({recommendation['band']})."
        payload = {
            "location": place,
            "date": resolved_date,
            "recommendation": recommendation,
            "forecast_day": day,
            "alerts": alerts,
            "alerts_status": alerts_status,
        }
        return payload, place, recommendation["band"], summary

    return _tool_call("get_travel_recommendation", location, body)


@mcp.tool
def get_severe_weather_alerts(location: str) -> dict:
    """
    Get active US National Weather Service alerts for a location. US-only.

    Args:
        location: A place name or "lat,lon" pair.

    Returns:
        A dict with status and, on success, coverage ("us_nws" or
        "unsupported_region" for non-US locations), alerts, and count.
    """
    def body():
        deadline = weather_client.new_deadline()
        place = _resolve_or_raise(location, deadline=deadline)
        alerts = weather_client.active_alerts(place, deadline=deadline)
        if alerts["coverage"] == "unsupported_region":
            summary = f"{place['label'] or place['name']}: no NWS alert coverage (non-US)."
        else:
            summary = f"{place['label'] or place['name']}: {alerts['count']} active alert(s)."
        return alerts, place, None, summary

    return _tool_call("get_severe_weather_alerts", location, body)


def _locations_label(locations) -> str:
    """
    Build the query-log label for a compare_locations call without risking
    a raise on a malformed argument. locations is only guaranteed to be a
    list of strings once body()'s own type guard has run, and that guard
    runs inside _tool_call's try block; this label is built before that,
    so it must tolerate anything.
    """
    try:
        return ",".join(str(loc) for loc in locations)
    except TypeError:
        return str(locations)


@mcp.tool
def compare_locations(locations: list[str], date: str | None = None) -> dict:
    """
    Rank 2-5 locations against each other for the same day, by travel score.

    Args:
        locations: 2-5 place names or "lat,lon" pairs.
        date: YYYY-MM-DD, within the next 16 days. None means today.

    Returns:
        A dict with status and, on success, `comparison` (ranked list, each
        entry also carrying `alerts_status`, best, worst) plus `failed`
        (locations that could not be resolved, could not be reached within
        this call's overall time budget, so the rest can still be ranked).
        On an invalid_request result (fewer than 2 locations resolved),
        `failed` is included too, so the agent can see why and retry with
        corrected names.
    """
    def body():
        if not isinstance(locations, list) or not all(isinstance(loc, str) and loc.strip() for loc in locations):
            raise InvalidRequestError("locations must be a list of non-empty strings.")
        if len(locations) > MAX_COMPARE_LOCATIONS:
            raise InvalidRequestError(f"Provide at most {MAX_COMPARE_LOCATIONS} locations to compare, got {len(locations)}.")

        # Up to 3 upstream calls per location; with 5 locations that is up
        # to 15 calls, which could otherwise chain to several minutes. One
        # shared deadline covers the whole loop, not just one location's
        # worth of calls, so a slow server burns through the remaining
        # locations quickly instead of being attempted at full cost.
        deadline = weather_client.new_deadline()

        entries = []
        failed = []
        for loc in locations:
            if time.monotonic() >= deadline:
                failed.append({"location": loc, "message": "Skipped: this comparison's overall time budget was already spent."})
                continue
            try:
                place = _resolve_or_raise(loc, deadline=deadline)
                day, resolved_date = weather_client.forecast_day_for_date(place, date, deadline=deadline)
                alerts_status = "ok"
                alerts = []
                try:
                    alerts_result = weather_client.active_alerts(place, deadline=deadline)
                    alerts = weather_client.filter_alerts_for_date(alerts_result["alerts"], resolved_date)
                except UpstreamAPIError:
                    logger.warning("Alerts lookup failed for %s; scoring without them.", place.get("label"))
                    alerts_status = "unavailable"
                label = place["label"] or place["name"]
                entries.append({"label": label, "day": day, "alerts": alerts, "alerts_status": alerts_status})
            except (LocationNotFoundError, InvalidRequestError, UpstreamAPIError) as exc:
                failed.append({"location": loc, "message": str(exc)})

        if len(entries) < 2:
            failed_names = ", ".join(f["location"] for f in failed) or "none"
            raise InvalidRequestError(
                f"Only {len(entries)} of {len(locations)} location(s) could be resolved; "
                f"need at least 2 to compare. Failed: {failed_names}.",
                context={"failed": failed},
            )

        comparison = recommendations.compare_days(entries)
        summary = f"Compared {len(entries)} location(s); best {comparison['best']}, worst {comparison['worst']}."
        payload = {"comparison": comparison, "failed": failed}
        return payload, None, comparison["best"], summary

    return _tool_call("compare_locations", _locations_label(locations), body)


@mcp.tool
def get_historical_weather(location: str, start_date: str, end_date: str) -> dict:
    """
    Get historical daily observations and an aggregate summary for a date range.

    Args:
        location: A place name or "lat,lon" pair.
        start_date: YYYY-MM-DD, start of the range (inclusive).
        end_date: YYYY-MM-DD, end of the range (inclusive). Must be at
            least 5 days before today (the archive's reporting lag), and
            the range must not exceed 366 days.

    Returns:
        A dict with status and, on success, location, timezone, `days`
        (one entry per day) and `summary` (avg_high/low_c/f,
        total_precipitation_mm/in, wettest/hottest/coldest_day, day_count).
    """
    def body():
        deadline = weather_client.new_deadline()
        place = _resolve_or_raise(location, deadline=deadline)
        history = weather_client.historical_daily(place, start_date, end_date, deadline=deadline)
        summary = f"{place['label'] or place['name']}: {history['summary']['day_count']} days of history from {start_date} to {end_date}."
        return history, place, None, summary

    return _tool_call("get_historical_weather", location, body)


if __name__ == "__main__":
    # Databricks Apps route external HTTP traffic to this port via app.yaml;
    # streamable-http is the transport Databricks' MCP client/gateway expects
    # (see the "Host your own MCP" doc linked in the module docstring above).
    #
    # fastmcp 3.4.6's FastMCP has no .app attribute (the reference repo's
    # `hasattr(mcp, 'app')` guard is dead code on this version); the
    # documented way to attach a Starlette ASGI middleware is the
    # `middleware=` kwarg on run()/run_http_async()/http_app(), which is
    # threaded straight into the Starlette app fastmcp builds.
    # Stateless mode: every request stands alone, with no server-side session to keep. A session
    # lives in one process's memory, so a stateful server only works while there is exactly one
    # replica and every request from a client lands on it. Databricks Apps can run more than one,
    # and the Unity Catalog `http_request` function used to test the connection cannot carry a
    # session at all. Stateless costs nothing here because no tool holds state between calls.
    port = int(os.getenv("DATABRICKS_APP_PORT", os.getenv("PORT", 8000)))
    app = mcp.http_app(
        middleware=[Middleware(ForwardedIdentityMiddleware)],
        stateless_http=True,
    )
    uvicorn.run(app, host="0.0.0.0", port=port)
