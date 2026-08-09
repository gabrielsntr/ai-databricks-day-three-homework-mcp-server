"""
Weather API adapter: Open-Meteo geocoding, forecast, and archive, plus NWS
active alerts.

This is the only module in this app that makes HTTP calls or touches
`requests`. weather_mcp_server.py stays thin - it calls the functions below
and turns their return values (or the exceptions they raise) into MCP tool
results. recommendations.py stays pure - it only ever sees the dicts this
module returns, never a live HTTP response.

Every public function returns a plain, JSON-serializable dict (or raises one
of the exceptions below). Callers do not need to know anything about
Open-Meteo's or NWS's response shapes.
"""

import base64
import logging
import os
import time
from datetime import date, datetime, timedelta

import requests

logger = logging.getLogger("weather-mcp.client")

GEOCODING_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
NWS_ALERTS_URL = "https://api.weather.gov/alerts/active"

CURRENT_FIELDS = (
    "temperature_2m,relative_humidity_2m,apparent_temperature,is_day,"
    "precipitation,weather_code,wind_speed_10m,wind_direction_10m,surface_pressure"
)
DAILY_FIELDS = (
    "weather_code,temperature_2m_max,temperature_2m_min,apparent_temperature_max,"
    "precipitation_sum,precipitation_probability_max,wind_speed_10m_max,"
    "wind_gusts_10m_max,uv_index_max,sunrise,sunset"
)
ARCHIVE_DAILY_FIELDS = "temperature_2m_max,temperature_2m_min,precipitation_sum,wind_speed_10m_max"

APP_NAME = os.environ.get("APP_NAME", "weather-mcp-server")

_RETRY_STATUSES = {429, 500, 502, 503, 504}
_RETRYABLE_ERRORS = (requests.ConnectionError, requests.Timeout)

_session = requests.Session()
_cached_user_agent: str | None = None

# How long, in total, a chain of weather_client calls is allowed to spend
# waiting on upstream APIs before giving up. A single hung request/retry
# pair already costs up to ~31s under the (5, 15) timeout below, and a
# tool that makes several calls (resolve, forecast, alerts, ...) can chain
# several of those back to back; without a shared cap the total can run
# well past what a gateway will hold a connection open for. See
# new_deadline() and the `deadline` parameter threaded through every
# public function in this module.
DEFAULT_UPSTREAM_BUDGET_S = 60.0


def new_deadline() -> float:
    """
    A time.monotonic() timestamp DEFAULT_UPSTREAM_BUDGET_S seconds from
    now. Callers that will make more than one weather_client call for a
    single logical operation (e.g. resolve a location, then fetch its
    forecast, then its alerts) should call this once and pass the result
    as `deadline` to each of those calls, so the whole chain shares one
    time budget instead of each call getting its own.
    """
    return time.monotonic() + DEFAULT_UPSTREAM_BUDGET_S


class WeatherClientError(Exception):
    """
    Base class for all errors this module raises.

    `context` is an optional dict of extra structured data the caller can
    fold into a tool's error result (e.g. compare_locations attaching the
    `failed` list to the InvalidRequestError it raises when too few
    locations resolved), on top of the plain string message every
    exception already carries. Empty by default, so nothing changes for
    callers that never pass it.
    """

    def __init__(self, message: str, *, context: dict | None = None):
        super().__init__(message)
        self.context = context or {}


class LocationNotFoundError(WeatherClientError):
    """No geocoding match was found for the given query."""


class UpstreamAPIError(WeatherClientError):
    """An upstream API timed out, returned a 5xx/429 after a retry, or sent a malformed payload."""


class InvalidRequestError(WeatherClientError):
    """The caller passed a bad argument (empty query, bad date range, out-of-range days, ...)."""


# WMO weather interpretation codes, https://open-meteo.com/en/docs - the full
# standard set, so describe_weather_code never has to guess.
WMO_WEATHER_CODES: dict[int, str] = {
    0: "Clear sky",
    1: "Mainly clear",
    2: "Partly cloudy",
    3: "Overcast",
    45: "Fog",
    48: "Depositing rime fog",
    51: "Light drizzle",
    53: "Moderate drizzle",
    55: "Dense drizzle",
    56: "Light freezing drizzle",
    57: "Dense freezing drizzle",
    61: "Slight rain",
    63: "Moderate rain",
    65: "Heavy rain",
    66: "Light freezing rain",
    67: "Heavy freezing rain",
    71: "Slight snow fall",
    73: "Moderate snow fall",
    75: "Heavy snow fall",
    77: "Snow grains",
    80: "Slight rain showers",
    81: "Moderate rain showers",
    82: "Violent rain showers",
    85: "Slight snow showers",
    86: "Heavy snow showers",
    95: "Thunderstorm",
    96: "Thunderstorm with slight hail",
    99: "Thunderstorm with heavy hail",
}

_COMPASS_POINTS = [
    "N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
    "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW",
]

# Indexed by date.weekday() (Monday=0 .. Sunday=6). A fixed table, not
# strftime("%A"), because strftime is locale-sensitive and the agent that
# reads these payloads needs an English name no matter what locale the
# server process happens to be running under.
_WEEKDAY_NAMES = (
    "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday",
)


def _weekday_for(date_str: str | None) -> str | None:
    """English weekday name (e.g. "Tuesday") for a YYYY-MM-DD string, or None if it is missing or malformed."""
    if not date_str:
        return None
    try:
        return _WEEKDAY_NAMES[date.fromisoformat(date_str).weekday()]
    except (ValueError, TypeError):
        return None


def describe_weather_code(code: int | None) -> str:
    """Translate a WMO weather code into a short human-readable phrase."""
    if code is None:
        return "Unknown"
    return WMO_WEATHER_CODES.get(code, "Unknown")


def _compass(deg: float | None) -> str | None:
    """Convert a wind direction in degrees to a 16-point compass label."""
    if deg is None:
        return None
    index = int((deg % 360) / 22.5 + 0.5) % 16
    return _COMPASS_POINTS[index]


def _c_to_f(celsius: float | None) -> float | None:
    return None if celsius is None else round(celsius * 9 / 5 + 32, 1)


def _kmh_to_mph(kmh: float | None) -> float | None:
    return None if kmh is None else round(kmh * 0.621371, 1)


def _mm_to_in(mm: float | None) -> float | None:
    return None if mm is None else round(mm * 0.0393701, 2)


def _contact_email() -> str:
    """
    Resolve the contact email used in the NWS User-Agent header.

    Order: a Databricks secret named by NWS_CONTACT_SECRET_SCOPE/
    NWS_CONTACT_SECRET_KEY, then the NWS_CONTACT_EMAIL env var, then a
    placeholder. The databricks.sdk import happens inside this function and
    any failure is swallowed back to the env-var path, so this module (and
    the whole server) imports fine with no Databricks credentials present.
    """
    scope = os.environ.get("NWS_CONTACT_SECRET_SCOPE")
    key = os.environ.get("NWS_CONTACT_SECRET_KEY")
    if scope and key:
        try:
            from databricks.sdk import WorkspaceClient

            secret = WorkspaceClient().secrets.get_secret(scope=scope, key=key)
            return base64.b64decode(secret.value).decode("utf-8")
        except Exception:
            logger.debug("NWS contact secret lookup failed, falling back to NWS_CONTACT_EMAIL.", exc_info=True)
    return os.environ.get("NWS_CONTACT_EMAIL", "weather-mcp@example.invalid")


def _user_agent() -> str:
    """Build (and cache for the process lifetime) the NWS-required User-Agent string."""
    global _cached_user_agent
    if _cached_user_agent is None:
        _cached_user_agent = f"{APP_NAME} ({_contact_email()})"
    return _cached_user_agent


def _request_with_retry(
    method: str, url: str, *, upstream: str, deadline: float | None = None, **kwargs
) -> requests.Response:
    """
    Issue one HTTP request with the shared retry policy: one retry, after a
    1s pause, on a connection error, timeout, 5xx, or 429. Any other status
    (including 4xx like 404) is returned as-is for the caller to interpret.
    Any other requests.RequestException (ChunkedEncodingError,
    TooManyRedirects, ContentDecodingError, ...) is not retried and raises
    UpstreamAPIError straight away.

    `deadline` is a time.monotonic() timestamp (see new_deadline()) after
    which no further attempt is made; a caller stringing several
    weather_client calls together passes the same deadline to each so the
    whole chain shares one budget. None (the default, and what every call
    in this module gets when nothing was threaded in) means "start a fresh
    DEFAULT_UPSTREAM_BUDGET_S budget for just this request." Either way,
    once the deadline has passed, this raises UpstreamAPIError immediately
    instead of sleeping or making another attempt - a spent budget is
    treated the same as a call that ran out of retries.
    """
    kwargs.setdefault("timeout", (5, 15))
    effective_deadline = deadline if deadline is not None else time.monotonic() + DEFAULT_UPSTREAM_BUDGET_S
    logger.info("%s %s", method, url)
    logger.debug("%s %s params=%s", method, url, kwargs.get("params"))
    attempt = 0
    while True:
        attempt += 1
        try:
            response = _session.request(method, url, **kwargs)
        except _RETRYABLE_ERRORS as exc:
            if attempt >= 2:
                raise UpstreamAPIError(f"{upstream} did not respond: {exc}") from exc
            if time.monotonic() >= effective_deadline:
                raise UpstreamAPIError(f"{upstream} did not respond and the time budget for this request is spent.") from exc
            time.sleep(1)
            continue
        except requests.RequestException as exc:
            # Anything else requests can raise (ChunkedEncodingError,
            # TooManyRedirects, ContentDecodingError, ...) is not one of
            # the two specific, retryable errors above. Not retried, but
            # still an upstream problem, not an unclassified crash.
            raise UpstreamAPIError(f"{upstream} request failed: {exc}") from exc
        if response.status_code in _RETRY_STATUSES and attempt < 2:
            if time.monotonic() >= effective_deadline:
                raise UpstreamAPIError(
                    f"{upstream} returned HTTP {response.status_code} and the time budget for this request is spent."
                )
            time.sleep(1)
            continue
        return response


def _get_json(
    url: str, *, params: dict, upstream: str, headers: dict | None = None, deadline: float | None = None
) -> dict:
    """
    GET a URL and return the parsed JSON body.

    Raises InvalidRequestError for a 4xx other than 429 (a bad argument we
    passed upstream, e.g. an out-of-range archive date), carrying whatever
    explanation the upstream body gave. Raises UpstreamAPIError for a 429,
    a 5xx, a bad body, or a spent time budget: those are the service's
    problem, not the caller's, and (other than the spent budget) are worth
    retrying.

    `deadline`: see _request_with_retry.
    """
    response = _request_with_retry("GET", url, upstream=upstream, params=params, headers=headers, deadline=deadline)
    if not response.ok:
        detail = ""
        try:
            body = response.json()
            detail = body.get("reason") or body.get("detail") or ""
        except ValueError:
            pass
        suffix = f" {detail}" if detail else ""
        if 400 <= response.status_code < 500 and response.status_code != 429:
            raise InvalidRequestError(f"{upstream} rejected the request (HTTP {response.status_code}).{suffix}")
        raise UpstreamAPIError(f"{upstream} returned HTTP {response.status_code}.{suffix}")
    try:
        return response.json()
    except ValueError as exc:
        raise UpstreamAPIError(f"{upstream} returned a malformed payload: {exc}") from exc


def _try_parse_coordinates(query: str) -> tuple[float, float] | None:
    """
    Parse a bare "lat,lon" pair. Returns None if the query is not shaped
    like a coordinate pair (so the caller falls back to geocoding), and
    raises InvalidRequestError if it is shaped like one but out of range.
    """
    parts = query.split(",")
    if len(parts) != 2:
        return None
    try:
        lat = float(parts[0].strip())
        lon = float(parts[1].strip())
    except ValueError:
        return None
    if not (-90.0 <= lat <= 90.0) or not (-180.0 <= lon <= 180.0):
        raise InvalidRequestError(f"Coordinates out of range: {query!r}.")
    return lat, lon


def _format_label(result: dict) -> str:
    parts = [result.get("name"), result.get("admin1"), result.get("country")]
    return ", ".join(part for part in parts if part)


def _pick_best_match(results: list[dict], query: str) -> dict:
    """Prefer the highest-population result whose name matches the query's first segment, else the first result."""
    primary = query.split(",")[0].strip().lower()
    named_matches = [r for r in results if (r.get("name") or "").strip().lower() == primary]
    if named_matches:
        return max(named_matches, key=lambda r: r.get("population") or 0)
    return results[0]


def resolve_location(query: str, *, deadline: float | None = None) -> dict:
    """
    Resolve free text ("Chicago", "Austin, TX", "Porto Alegre, Brazil") or a
    bare "lat,lon" pair into a location dict. Raises InvalidRequestError for
    an empty query and LocationNotFoundError when geocoding finds nothing.

    `deadline`: see _request_with_retry. Only relevant when this call
    reaches the network; the coordinate fast path below never does.
    """
    if query is None or not query.strip():
        raise InvalidRequestError("Location query cannot be empty.")
    query = query.strip()

    coords = _try_parse_coordinates(query)
    if coords is not None:
        lat, lon = coords
        name = f"{lat}, {lon}"
        return {
            "name": name,
            "admin1": None,
            "country": None,
            "country_code": None,
            "latitude": lat,
            "longitude": lon,
            "timezone": "auto",
            "label": name,
            "source": "coordinates",
            "alternatives": [],
        }

    data = _get_json(
        GEOCODING_URL,
        params={"name": query, "count": 5, "language": "en", "format": "json"},
        upstream="open-meteo-geocoding",
        deadline=deadline,
    )
    results = data.get("results") or []
    if not results:
        raise LocationNotFoundError(f"No location found for {query!r}.")

    best = _pick_best_match(results, query)
    alternatives = [_format_label(r) for r in results if r.get("id") != best.get("id")][:4]

    return {
        "name": best.get("name"),
        "admin1": best.get("admin1"),
        "country": best.get("country"),
        "country_code": best.get("country_code"),
        "latitude": best.get("latitude"),
        "longitude": best.get("longitude"),
        "timezone": best.get("timezone", "auto"),
        "label": _format_label(best),
        "source": "open-meteo-geocoding",
        "alternatives": alternatives,
    }


def current_conditions(place: dict, *, deadline: float | None = None) -> dict:
    """Fetch current conditions for a resolved place dict (as returned by resolve_location). `deadline`: see _request_with_retry."""
    data = _get_json(
        FORECAST_URL,
        params={"latitude": place["latitude"], "longitude": place["longitude"], "timezone": "auto", "current": CURRENT_FIELDS},
        upstream="open-meteo-forecast",
        deadline=deadline,
    )
    current = data.get("current")
    if not isinstance(current, dict):
        raise UpstreamAPIError("open-meteo-forecast returned a payload with no 'current' block.")

    temp_c = current.get("temperature_2m")
    feels_c = current.get("apparent_temperature")
    precip_mm = current.get("precipitation")
    wind_kmh = current.get("wind_speed_10m")
    wind_dir = current.get("wind_direction_10m")
    weather_code = current.get("weather_code")
    is_day = current.get("is_day")

    observed_at = current.get("time")
    # observed_at is a local timestamp string like "2026-08-09T14:00"; the
    # date part is everything before the "T".
    observed_date = observed_at.split("T", 1)[0] if isinstance(observed_at, str) else None

    return {
        "location": place,
        "observed_at": observed_at,
        "weekday": _weekday_for(observed_date),
        "timezone": data.get("timezone", place.get("timezone")),
        "temperature_c": temp_c,
        "temperature_f": _c_to_f(temp_c),
        "feels_like_c": feels_c,
        "feels_like_f": _c_to_f(feels_c),
        "humidity_pct": current.get("relative_humidity_2m"),
        "precipitation_mm": precip_mm,
        "precipitation_in": _mm_to_in(precip_mm),
        "wind_speed_kmh": wind_kmh,
        "wind_speed_mph": _kmh_to_mph(wind_kmh),
        "wind_direction_deg": wind_dir,
        "wind_direction_compass": _compass(wind_dir),
        "pressure_hpa": current.get("surface_pressure"),
        "is_day": None if is_day is None else bool(is_day),
        "weather_code": weather_code,
        "conditions": describe_weather_code(weather_code),
    }


def _daily_field(daily: dict, name: str, index: int):
    values = daily.get(name)
    if not values or index >= len(values):
        return None
    return values[index]


def daily_forecast(place: dict, days: int, *, deadline: float | None = None) -> dict:
    """Fetch an N-day daily forecast (1-16 days) for a resolved place dict. `deadline`: see _request_with_retry."""
    if not isinstance(days, int) or isinstance(days, bool) or not (1 <= days <= 16):
        raise InvalidRequestError(f"days must be an integer between 1 and 16, got {days!r}.")

    data = _get_json(
        FORECAST_URL,
        params={
            "latitude": place["latitude"],
            "longitude": place["longitude"],
            "timezone": "auto",
            "daily": DAILY_FIELDS,
            "forecast_days": days,
        },
        upstream="open-meteo-forecast",
        deadline=deadline,
    )
    daily = data.get("daily") or {}
    dates = daily.get("time") or []

    days_out = []
    for i, day_date in enumerate(dates):
        weather_code = _daily_field(daily, "weather_code", i)
        temp_high_c = _daily_field(daily, "temperature_2m_max", i)
        temp_low_c = _daily_field(daily, "temperature_2m_min", i)
        feels_high_c = _daily_field(daily, "apparent_temperature_max", i)
        precip_mm = _daily_field(daily, "precipitation_sum", i)
        wind_max_kmh = _daily_field(daily, "wind_speed_10m_max", i)
        wind_gust_max_kmh = _daily_field(daily, "wind_gusts_10m_max", i)
        days_out.append({
            "date": day_date,
            "weekday": _weekday_for(day_date),
            "weather_code": weather_code,
            "conditions": describe_weather_code(weather_code),
            "temp_high_c": temp_high_c,
            "temp_high_f": _c_to_f(temp_high_c),
            "temp_low_c": temp_low_c,
            "temp_low_f": _c_to_f(temp_low_c),
            "feels_like_high_c": feels_high_c,
            "feels_like_high_f": _c_to_f(feels_high_c),
            "precipitation_mm": precip_mm,
            "precipitation_in": _mm_to_in(precip_mm),
            "precipitation_chance_pct": _daily_field(daily, "precipitation_probability_max", i),
            "wind_max_kmh": wind_max_kmh,
            "wind_max_mph": _kmh_to_mph(wind_max_kmh),
            "wind_gust_max_kmh": wind_gust_max_kmh,
            "wind_gust_max_mph": _kmh_to_mph(wind_gust_max_kmh),
            "uv_index_max": _daily_field(daily, "uv_index_max", i),
            "sunrise": _daily_field(daily, "sunrise", i),
            "sunset": _daily_field(daily, "sunset", i),
        })

    return {"location": place, "timezone": data.get("timezone", place.get("timezone")), "days": days_out}


def forecast_day_for_date(place: dict, target: str | None, *, deadline: float | None = None) -> tuple[dict, str]:
    """
    Fetch the full 16-day forecast window for a resolved place dict and
    pick the day matching `target`.

    The match is against the `date` string Open-Meteo actually returned for
    each day, never against a day count computed from the server's clock:
    the server's date is not the location's date, and Open-Meteo's
    timezone=auto window does not necessarily start "today" from the
    server's point of view.

    Args:
        place: A resolved location dict, as returned by resolve_location.
        target: YYYY-MM-DD, or None for the first day in the window (which
            Open-Meteo, with timezone=auto, defines as today in the
            location's own timezone).
        deadline: see _request_with_retry.

    Returns:
        A tuple of (the matched day dict, that day's own `date` value), so
        the label returned and the data returned can never disagree.

    Raises:
        InvalidRequestError: `target` is not YYYY-MM-DD, or does not match
            any date in the forecast window. The message names the real
            window from the response, not a server-relative one.
    """
    forecast = daily_forecast(place, 16, deadline=deadline)
    days = forecast["days"]

    if target is None:
        if not days:
            raise UpstreamAPIError("The forecast response did not include any days.")
        return days[0], days[0]["date"]

    try:
        date.fromisoformat(target)
    except (ValueError, TypeError):
        raise InvalidRequestError(f"date must be YYYY-MM-DD, got {target!r}.")

    for day in days:
        if day["date"] == target:
            return day, day["date"]

    if days:
        window = f"{days[0]['date']} to {days[-1]['date']}"
    else:
        window = "an empty window"
    raise InvalidRequestError(f"date must be within the forecast window ({window}), got {target!r}.")


def _validate_date_range(start_date: str, end_date: str) -> tuple[date, date]:
    try:
        start = date.fromisoformat(start_date)
    except (ValueError, TypeError):
        raise InvalidRequestError(f"start_date must be YYYY-MM-DD, got {start_date!r}.")
    try:
        end = date.fromisoformat(end_date)
    except (ValueError, TypeError):
        raise InvalidRequestError(f"end_date must be YYYY-MM-DD, got {end_date!r}.")

    if start > end:
        raise InvalidRequestError(f"start_date {start_date} must not be after end_date {end_date}.")

    latest_allowed = date.today() - timedelta(days=5)
    if end > latest_allowed:
        raise InvalidRequestError(
            f"end_date must be on or before {latest_allowed.isoformat()}, the archive lags about 5 days behind today."
        )
    if (end - start).days > 366:
        raise InvalidRequestError("Date range cannot span more than 366 days.")

    return start, end


def _summarize_history(days: list[dict]) -> dict:
    if not days:
        return {
            "avg_high_c": None, "avg_high_f": None,
            "avg_low_c": None, "avg_low_f": None,
            "total_precipitation_mm": None, "total_precipitation_in": None,
            "wettest_day": None, "hottest_day": None, "coldest_day": None,
            "day_count": 0,
        }

    highs = [d["temp_high_c"] for d in days if d["temp_high_c"] is not None]
    lows = [d["temp_low_c"] for d in days if d["temp_low_c"] is not None]
    precs = [d["precipitation_mm"] for d in days if d["precipitation_mm"] is not None]

    avg_high_c = round(sum(highs) / len(highs), 1) if highs else None
    avg_low_c = round(sum(lows) / len(lows), 1) if lows else None
    total_precip_mm = round(sum(precs), 1) if precs else None

    wettest = max((d for d in days if d["precipitation_mm"] is not None), key=lambda d: d["precipitation_mm"], default=None)
    hottest = max((d for d in days if d["temp_high_c"] is not None), key=lambda d: d["temp_high_c"], default=None)
    coldest = min((d for d in days if d["temp_low_c"] is not None), key=lambda d: d["temp_low_c"], default=None)

    return {
        "avg_high_c": avg_high_c, "avg_high_f": _c_to_f(avg_high_c),
        "avg_low_c": avg_low_c, "avg_low_f": _c_to_f(avg_low_c),
        "total_precipitation_mm": total_precip_mm, "total_precipitation_in": _mm_to_in(total_precip_mm),
        "wettest_day": wettest["date"] if wettest else None,
        "hottest_day": hottest["date"] if hottest else None,
        "coldest_day": coldest["date"] if coldest else None,
        "day_count": len(days),
    }


def historical_daily(place: dict, start_date: str, end_date: str, *, deadline: float | None = None) -> dict:
    """
    Fetch historical daily observations for a resolved place dict. Validates
    that both dates are ISO YYYY-MM-DD, start <= end, end is at least 5 days
    in the past (the archive's lag), and the span is at most 366 days.

    `deadline`: see _request_with_retry.
    """
    start, end = _validate_date_range(start_date, end_date)

    data = _get_json(
        ARCHIVE_URL,
        params={
            "latitude": place["latitude"],
            "longitude": place["longitude"],
            "start_date": start.isoformat(),
            "end_date": end.isoformat(),
            "timezone": "auto",
            "daily": ARCHIVE_DAILY_FIELDS,
        },
        upstream="open-meteo-archive",
        deadline=deadline,
    )
    daily = data.get("daily") or {}
    dates = daily.get("time") or []

    days_out = []
    for i, day_date in enumerate(dates):
        temp_high_c = _daily_field(daily, "temperature_2m_max", i)
        temp_low_c = _daily_field(daily, "temperature_2m_min", i)
        precip_mm = _daily_field(daily, "precipitation_sum", i)
        wind_max_kmh = _daily_field(daily, "wind_speed_10m_max", i)
        days_out.append({
            "date": day_date,
            "weekday": _weekday_for(day_date),
            "temp_high_c": temp_high_c,
            "temp_high_f": _c_to_f(temp_high_c),
            "temp_low_c": temp_low_c,
            "temp_low_f": _c_to_f(temp_low_c),
            "precipitation_mm": precip_mm,
            "precipitation_in": _mm_to_in(precip_mm),
            "wind_max_kmh": wind_max_kmh,
            "wind_max_mph": _kmh_to_mph(wind_max_kmh),
        })

    return {
        "location": place,
        "timezone": data.get("timezone", place.get("timezone")),
        "days": days_out,
        "summary": _summarize_history(days_out),
    }


def _get_nws_alerts(lat: float, lon: float, *, deadline: float | None = None) -> dict | None:
    """
    GET active NWS alerts for a point. Returns None when NWS has no
    coverage for the point, so active_alerts can treat it as "no coverage"
    instead of an error.

    NWS signals "no coverage" two different ways depending on the point:
    a plain 404, or a 400 whose body says the point is out of bounds. Both
    mean the same thing here. A 400 with any other body is a genuinely
    malformed request and still raises.

    `deadline`: see _request_with_retry.
    """
    response = _request_with_retry(
        "GET",
        NWS_ALERTS_URL,
        upstream="nws-alerts",
        params={"point": f"{lat},{lon}"},
        headers={"User-Agent": _user_agent(), "Accept": "application/geo+json"},
        deadline=deadline,
    )
    if response.status_code == 404:
        return None
    if response.status_code == 400:
        detail = ""
        try:
            detail = (response.json() or {}).get("detail", "")
        except ValueError:
            pass
        if "out of bounds" in detail.lower():
            return None
    if not response.ok:
        raise UpstreamAPIError(f"nws-alerts returned HTTP {response.status_code}.")
    try:
        return response.json()
    except ValueError as exc:
        raise UpstreamAPIError(f"nws-alerts returned a malformed payload: {exc}") from exc


def _parse_alert(props: dict) -> dict:
    description = props.get("description") or ""
    if len(description) > 600:
        description = description[:600] + "…"
    return {
        "event": props.get("event"),
        "severity": props.get("severity"),
        "urgency": props.get("urgency"),
        "certainty": props.get("certainty"),
        "headline": props.get("headline"),
        "area": props.get("areaDesc"),
        "onset": props.get("onset"),
        "ends": props.get("ends"),
        "expires": props.get("expires"),
        "instruction": props.get("instruction"),
        "description": description,
        "sender": props.get("senderName"),
    }


def active_alerts(place: dict, *, deadline: float | None = None) -> dict:
    """
    Fetch active NWS alerts for a resolved place dict. US-only. Coverage is
    a pure function of the NWS response: no coverage (see _get_nws_alerts)
    means coverage="unsupported_region"; any other response means
    coverage="us_nws", including an empty feature list, which just means no
    alerts are active there right now.

    This used to also treat an empty-but-200 response as unsupported when
    place["country_code"] was set and not "US". That heuristic is gone: US
    territories (Puerto Rico, Guam, ...) get a country_code of "PR", "GU",
    etc, not "US", so it reported them as unsupported even though NWS
    actively serves them. The HTTP status alone is what NWS itself uses to
    say whether a point is covered, so trust that instead.

    `deadline`: see _request_with_retry.
    """
    data = _get_nws_alerts(place["latitude"], place["longitude"], deadline=deadline)
    if data is None:
        return {"location": place, "coverage": "unsupported_region", "alerts": [], "count": 0}

    features = data.get("features") or []
    alerts = [_parse_alert(f.get("properties") or {}) for f in features]
    return {"location": place, "coverage": "us_nws", "alerts": alerts, "count": len(alerts)}


def _parse_alert_timestamp(value: str | None) -> datetime | None:
    """Parse an NWS alert's ISO 8601 (with offset) timestamp. None on missing or malformed input, never raises."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def filter_alerts_for_date(alerts: list[dict], target_date: str) -> list[dict]:
    """
    Drop alerts that are already over by the start of a given day, so a
    future day is not penalised for an alert that expires before it
    arrives.

    An alert is dropped when its `ends` or `expires` timestamp (as parsed
    by _parse_alert) resolves to a calendar date before `target_date`. An
    alert with neither field set is kept: no stated end means it is still
    live. A field that fails to parse is treated the same as missing,
    never dropped on account of a malformed timestamp.

    Args:
        alerts: A list of alert dicts as returned in active_alerts()["alerts"].
        target_date: YYYY-MM-DD, the day being scored.

    Returns:
        The subset of `alerts` still relevant to `target_date`.
    """
    target = date.fromisoformat(target_date)

    def is_live(alert: dict) -> bool:
        for field in ("ends", "expires"):
            ts = _parse_alert_timestamp(alert.get(field))
            if ts is not None and ts.date() < target:
                return False
        return True

    return [a for a in alerts if is_live(a)]
