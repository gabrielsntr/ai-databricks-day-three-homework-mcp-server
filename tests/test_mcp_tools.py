"""
Tests for weather_mcp_server.py: the @mcp.tool wrappers.

Invocation note: in the installed fastmcp (3.4.6), `@mcp.tool` registers the
function with the FastMCP server but returns the original plain function
unchanged (verified: `type(decorated_fn) is function`, and calling it runs
the function body directly, synchronously, with normal keyword arguments).
So these tests call the tools the same way any other Python code would,
e.g. `weather_mcp_server.resolve_location(query="Chicago")` - no MCP client,
no event loop, no `.fn`/`.run()` indirection needed.

Every test here monkeypatches weather_client's functions (the only module
that touches the network) so no tool call ever reaches Open-Meteo, NWS, or
Lakebase.
"""

import pytest

import query_log
import weather_client
import weather_mcp_server
from weather_client import InvalidRequestError, LocationNotFoundError, UpstreamAPIError


def _place(**overrides):
    base = {
        "name": "Chicago",
        "admin1": "Illinois",
        "country": "United States",
        "country_code": "US",
        "latitude": 41.85,
        "longitude": -87.65,
        "timezone": "America/Chicago",
        "label": "Chicago, Illinois, United States",
        "source": "open-meteo-geocoding",
        "alternatives": [],
    }
    base.update(overrides)
    return base


def _day(**overrides):
    base = {
        "date": "2026-08-09",
        "weather_code": 61,
        "conditions": "Slight rain",
        "temp_high_c": 22.0, "temp_high_f": 71.6,
        "temp_low_c": 14.0, "temp_low_f": 57.2,
        "feels_like_high_c": 23.0, "feels_like_high_f": 73.4,
        "precipitation_mm": 3.0, "precipitation_in": 0.12,
        "precipitation_chance_pct": 55,
        "wind_max_kmh": 18.0, "wind_max_mph": 11.2,
        "wind_gust_max_kmh": 30.0, "wind_gust_max_mph": 18.6,
        "uv_index_max": 5.0,
        "sunrise": "2026-08-09T05:58",
        "sunset": "2026-08-09T20:07",
    }
    base.update(overrides)
    return base


def _current(**overrides):
    base = {
        "location": _place(),
        "observed_at": "2026-08-09T14:00",
        "timezone": "America/Chicago",
        "temperature_c": 27.4, "temperature_f": 81.3,
        "feels_like_c": 29.1, "feels_like_f": 84.4,
        "humidity_pct": 63,
        "precipitation_mm": 0.0, "precipitation_in": 0.0,
        "wind_speed_kmh": 14.8, "wind_speed_mph": 9.2,
        "wind_direction_deg": 202, "wind_direction_compass": "SSW",
        "pressure_hpa": 1012.3,
        "is_day": True,
        "weather_code": 2, "conditions": "Partly cloudy",
    }
    base.update(overrides)
    return base


@pytest.fixture(autouse=True)
def stub_query_log(monkeypatch):
    """query_log talks to Lakebase; keep it a harmless no-op unless a test overrides it."""
    monkeypatch.setattr(query_log, "record", lambda *a, **k: None)


def _stub_resolve(monkeypatch, place=None, error=None):
    def fake(location, deadline=None):
        if error is not None:
            raise error
        return place if place is not None else _place()
    monkeypatch.setattr(weather_client, "resolve_location", fake)


def _stub_daily_forecast(monkeypatch, day=None):
    """
    Every returned day carries the same date (2026-08-09, _day()'s
    default), which is what forecast_day_for_date returns for target=None.
    A test that needs to exercise a specific target date should pass a
    matching `day` (or monkeypatch daily_forecast itself for more control).
    """
    def fake(place, days, deadline=None):
        return {"location": place, "timezone": "America/Chicago", "days": [day or _day() for _ in range(days)]}
    monkeypatch.setattr(weather_client, "daily_forecast", fake)


# ---------------------------------------------------------------------------
# error taxonomy
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "raised, expected_status",
    [
        (LocationNotFoundError("no such place"), "not_found"),
        (InvalidRequestError("bad query"), "invalid_request"),
        (UpstreamAPIError("upstream boom"), "upstream_error"),
        (RuntimeError("unexpected"), "error"),
    ],
)
def test_error_taxonomy_maps_to_expected_status(monkeypatch, raised, expected_status):
    _stub_resolve(monkeypatch, error=raised)
    result = weather_mcp_server.resolve_location(query="Nowhereville")
    assert result["status"] == expected_status
    assert "message" in result
    assert result["message"] == str(raised)


def test_not_found_result_includes_a_disambiguation_hint(monkeypatch):
    _stub_resolve(monkeypatch, error=LocationNotFoundError("no match"))
    result = weather_mcp_server.resolve_location(query="Nowhereville")
    assert result["status"] == "not_found"
    assert "hint" in result


ALL_TOOL_CALLS = [
    ("resolve_location", {"query": "Chicago"}),
    ("get_current_weather", {"location": "Chicago"}),
    ("get_forecast", {"location": "Chicago", "days": 3}),
    ("get_umbrella_advice", {"location": "Chicago", "date": None}),
    ("get_travel_recommendation", {"location": "Chicago", "date": None}),
    ("get_severe_weather_alerts", {"location": "Chicago"}),
    ("compare_locations", {"locations": ["Chicago", "Austin"], "date": None}),
    ("get_historical_weather", {"location": "Chicago", "start_date": "2020-01-01", "end_date": "2020-01-02"}),
]


@pytest.mark.parametrize("tool_name, kwargs", ALL_TOOL_CALLS, ids=[t[0] for t in ALL_TOOL_CALLS])
def test_no_tool_ever_raises_when_resolve_location_blows_up(monkeypatch, tool_name, kwargs):
    _stub_resolve(monkeypatch, error=RuntimeError("simulated failure"))
    tool = getattr(weather_mcp_server, tool_name)
    result = tool(**kwargs)  # must not raise
    assert isinstance(result, dict)
    assert "status" in result


# ---------------------------------------------------------------------------
# success payloads
# ---------------------------------------------------------------------------

def test_resolve_location_success_is_ok(monkeypatch):
    _stub_resolve(monkeypatch)
    result = weather_mcp_server.resolve_location(query="Chicago")
    assert result["status"] == "ok"
    assert result["name"] == "Chicago"


def test_get_current_weather_success_is_ok(monkeypatch):
    _stub_resolve(monkeypatch)
    monkeypatch.setattr(weather_client, "current_conditions", lambda place, deadline=None: _current())
    result = weather_mcp_server.get_current_weather(location="Chicago")
    assert result["status"] == "ok"
    assert result["temperature_c"] == 27.4


def test_get_forecast_success_is_ok(monkeypatch):
    _stub_resolve(monkeypatch)
    monkeypatch.setattr(
        weather_client, "daily_forecast",
        lambda place, days, deadline=None: {"location": place, "timezone": "America/Chicago", "days": [_day() for _ in range(days)]},
    )
    result = weather_mcp_server.get_forecast(location="Chicago", days=3)
    assert result["status"] == "ok"
    assert len(result["days"]) == 3


def test_get_umbrella_advice_success_is_ok(monkeypatch):
    _stub_resolve(monkeypatch)
    _stub_daily_forecast(monkeypatch, day=_day(precipitation_chance_pct=70))
    result = weather_mcp_server.get_umbrella_advice(location="Chicago", date=None)
    assert result["status"] == "ok"
    assert result["advice"]["verdict"] == "yes"
    assert result["forecast_day"]["precipitation_chance_pct"] == 70


def test_get_umbrella_advice_rejects_a_date_outside_the_16_day_window(monkeypatch):
    # forecast_day_for_date (F3) always fetches the real window first, so
    # the stub must supply one; the fake window's only date is 2026-08-09
    # (see _day()), well short of 2099-01-01.
    _stub_resolve(monkeypatch)
    _stub_daily_forecast(monkeypatch)
    result = weather_mcp_server.get_umbrella_advice(location="Chicago", date="2099-01-01")
    assert result["status"] == "invalid_request"
    assert "2026-08-09" in result["message"]


def test_get_travel_recommendation_success_is_ok(monkeypatch):
    _stub_resolve(monkeypatch)
    _stub_daily_forecast(monkeypatch)
    monkeypatch.setattr(
        weather_client, "active_alerts",
        lambda place, deadline=None: {"location": place, "coverage": "us_nws", "alerts": [], "count": 0},
    )
    result = weather_mcp_server.get_travel_recommendation(location="Chicago", date=None)
    assert result["status"] == "ok"
    assert result["alerts_status"] == "ok"
    assert result["alerts"] == []
    assert 0 <= result["recommendation"]["score"] <= 100


def test_get_travel_recommendation_folds_a_live_alert_into_the_payload(monkeypatch):
    """payload["alerts"] (F5) carries the parsed alerts, not just a count, so the
    agent can state event/severity/area/instruction per the system prompt."""
    _stub_resolve(monkeypatch)
    _stub_daily_forecast(monkeypatch, day=_day(date="2026-08-09"))
    live_alert = {
        "event": "Severe Thunderstorm Warning", "severity": "Severe", "area": "Cook County",
        "instruction": "Seek shelter now.", "ends": None, "expires": None,
    }
    monkeypatch.setattr(
        weather_client, "active_alerts",
        lambda place, deadline=None: {"location": place, "coverage": "us_nws", "alerts": [live_alert], "count": 1},
    )
    result = weather_mcp_server.get_travel_recommendation(location="Chicago", date=None)
    assert result["status"] == "ok"
    assert result["alerts"] == [live_alert]


def test_get_travel_recommendation_excludes_an_alert_that_already_expired(monkeypatch):
    """F5: an alert whose expires/ends is before the scored day must not reach travel_score."""
    _stub_resolve(monkeypatch)
    _stub_daily_forecast(monkeypatch, day=_day(date="2026-08-23", precipitation_chance_pct=0, precipitation_mm=0.0, wind_max_kmh=0.0))
    expired_alert = {"event": "Flood Advisory", "severity": "Minor", "ends": "2026-08-13T18:00:00-04:00", "expires": None}
    monkeypatch.setattr(
        weather_client, "active_alerts",
        lambda place, deadline=None: {"location": place, "coverage": "us_nws", "alerts": [expired_alert], "count": 1},
    )
    result = weather_mcp_server.get_travel_recommendation(location="Chicago", date="2026-08-23")
    assert result["status"] == "ok"
    assert result["alerts"] == []
    assert result["recommendation"]["score"] == 100


def test_get_travel_recommendation_degrades_when_alerts_call_fails(monkeypatch):
    _stub_resolve(monkeypatch)
    _stub_daily_forecast(monkeypatch)

    def boom(place, deadline=None):
        raise UpstreamAPIError("nws-alerts returned HTTP 503.")

    monkeypatch.setattr(weather_client, "active_alerts", boom)
    result = weather_mcp_server.get_travel_recommendation(location="Chicago", date=None)
    assert result["status"] == "ok"
    assert result["alerts_status"] == "unavailable"
    assert result["alerts"] == []


def test_get_severe_weather_alerts_success_is_ok(monkeypatch):
    _stub_resolve(monkeypatch)
    monkeypatch.setattr(
        weather_client, "active_alerts",
        lambda place, deadline=None: {"location": place, "coverage": "us_nws", "alerts": [], "count": 0},
    )
    result = weather_mcp_server.get_severe_weather_alerts(location="Chicago")
    assert result["status"] == "ok"
    assert result["coverage"] == "us_nws"


def test_get_historical_weather_success_is_ok(monkeypatch):
    _stub_resolve(monkeypatch)
    summary = {
        "avg_high_c": 25.0, "avg_high_f": 77.0, "avg_low_c": 15.0, "avg_low_f": 59.0,
        "total_precipitation_mm": 10.0, "total_precipitation_in": 0.39,
        "wettest_day": "2020-01-01", "hottest_day": "2020-01-01", "coldest_day": "2020-01-02",
        "day_count": 2,
    }
    monkeypatch.setattr(
        weather_client, "historical_daily",
        lambda place, start_date, end_date, deadline=None: {"location": place, "timezone": "America/Chicago", "days": [], "summary": summary},
    )
    result = weather_mcp_server.get_historical_weather(location="Chicago", start_date="2020-01-01", end_date="2020-01-02")
    assert result["status"] == "ok"
    assert result["summary"]["day_count"] == 2


def test_get_historical_weather_upstream_400_is_invalid_request_with_reason(monkeypatch):
    """F4: an Open-Meteo 4xx with a `reason` body maps to invalid_request, reason preserved."""
    _stub_resolve(monkeypatch)

    def boom(place, start_date, end_date, deadline=None):
        raise InvalidRequestError(
            "open-meteo-archive rejected the request (HTTP 400). "
            "Parameter 'start_date' is out of allowed range from 1940-01-01 to 2026-08-09"
        )

    monkeypatch.setattr(weather_client, "historical_daily", boom)
    result = weather_mcp_server.get_historical_weather(location="Chicago", start_date="1930-01-01", end_date="1930-01-05")
    assert result["status"] == "invalid_request"
    assert "1940-01-01" in result["message"]


def test_compare_locations_success_ranks_resolved_and_reports_failed(monkeypatch):
    def fake_resolve(location, deadline=None):
        if location == "Nowhereville":
            raise LocationNotFoundError(f"No location found for {location!r}.")
        return _place(name=location, label=f"{location}, Illinois, United States")

    monkeypatch.setattr(weather_client, "resolve_location", fake_resolve)
    _stub_daily_forecast(monkeypatch)
    monkeypatch.setattr(
        weather_client, "active_alerts",
        lambda place, deadline=None: {"location": place, "coverage": "us_nws", "alerts": [], "count": 0},
    )

    result = weather_mcp_server.compare_locations(locations=["Chicago", "Nowhereville", "Austin"], date=None)

    assert result["status"] == "ok"
    ranked_labels = {r["label"] for r in result["comparison"]["ranked"]}
    assert ranked_labels == {"Chicago, Illinois, United States", "Austin, Illinois, United States"}
    assert len(result["failed"]) == 1
    assert result["failed"][0]["location"] == "Nowhereville"


def test_compare_locations_rejects_fewer_than_two_survivors(monkeypatch):
    """F6: only one location resolves; must not silently rank it against itself."""
    _stub_resolve(monkeypatch)
    _stub_daily_forecast(monkeypatch)
    monkeypatch.setattr(
        weather_client, "active_alerts",
        lambda place, deadline=None: {"location": place, "coverage": "us_nws", "alerts": [], "count": 0},
    )
    result = weather_mcp_server.compare_locations(locations=["Chicago"], date=None)
    assert result["status"] == "invalid_request"
    assert "failed" in result


def test_compare_locations_single_survivor_out_of_several_is_invalid_request(monkeypatch):
    """F6: 2 requested, only 1 resolves; the failed one must be named in the result."""
    def fake_resolve(location, deadline=None):
        if location == "Nowhereville":
            raise LocationNotFoundError(f"No location found for {location!r}.")
        return _place()

    monkeypatch.setattr(weather_client, "resolve_location", fake_resolve)
    _stub_daily_forecast(monkeypatch)
    monkeypatch.setattr(
        weather_client, "active_alerts",
        lambda place, deadline=None: {"location": place, "coverage": "us_nws", "alerts": [], "count": 0},
    )

    result = weather_mcp_server.compare_locations(locations=["Chicago", "Nowhereville"], date=None)

    assert result["status"] == "invalid_request"
    assert result["failed"] == [{"location": "Nowhereville", "message": "No location found for 'Nowhereville'."}]


def test_compare_locations_rejects_more_than_five(monkeypatch):
    _stub_resolve(monkeypatch)
    result = weather_mcp_server.compare_locations(locations=["A", "B", "C", "D", "E", "F"], date=None)
    assert result["status"] == "invalid_request"


def test_compare_locations_rejects_a_non_list_argument(monkeypatch):
    """F6: @mcp.tool returns the plain function in fastmcp 3.4.6, so in-process
    callers (tests, dashboard) bypass MCP/pydantic validation and can hit this."""
    result = weather_mcp_server.compare_locations(locations="Chicago,Austin", date=None)
    assert result["status"] == "invalid_request"


def test_compare_locations_rejects_a_blank_location_string(monkeypatch):
    result = weather_mcp_server.compare_locations(locations=["Chicago", "   "], date=None)
    assert result["status"] == "invalid_request"


# ---------------------------------------------------------------------------
# query_log never breaks a tool call
# ---------------------------------------------------------------------------

def test_query_log_record_failure_does_not_break_the_tool_call(monkeypatch):
    _stub_resolve(monkeypatch)
    monkeypatch.setattr(weather_client, "current_conditions", lambda place, deadline=None: _current())

    def raising_record(*args, **kwargs):
        raise RuntimeError("Lakebase is on fire")

    monkeypatch.setattr(query_log, "record", raising_record)

    result = weather_mcp_server.get_current_weather(location="Chicago")

    assert result["status"] == "ok"
    assert result["temperature_c"] == 27.4
