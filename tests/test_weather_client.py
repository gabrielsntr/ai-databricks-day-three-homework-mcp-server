"""
Tests for weather_client.py: parsing of canned Open-Meteo/NWS payloads,
location resolution, date validation, and the retry policy. Every HTTP call
is faked at the requests.Session boundary (weather_client._session.request)
via RecordingTransport below; nothing here ever reaches the network.
"""

from datetime import date, timedelta

import pytest

import weather_client
from weather_client import (
    InvalidRequestError,
    LocationNotFoundError,
    UpstreamAPIError,
)


class FakeResponse:
    """Stand-in for requests.Response, only the bits weather_client reads."""

    def __init__(self, status_code=200, json_data=None):
        self.status_code = status_code
        self._json_data = {} if json_data is None else json_data
        self.ok = 200 <= status_code < 400

    def json(self):
        return self._json_data


class RecordingTransport:
    """
    Fake stand-in for _session.request. Consumes one entry from `responses`
    per call, in order, and records every call's kwargs so tests can assert
    on params/headers/call count. Raising past the end of `responses` means
    the code under test made more requests than the test expected.
    """

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    def __call__(self, method, url, **kwargs):
        self.calls.append({"method": method, "url": url, **kwargs})
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    @property
    def call_count(self):
        return len(self.calls)


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


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    """Every retry test pauses 1s between attempts in real life; skip that here."""
    monkeypatch.setattr(weather_client.time, "sleep", lambda seconds: None)


@pytest.fixture(autouse=True)
def reset_user_agent_cache():
    """_user_agent() caches its result process-wide; do not let tests leak into each other."""
    weather_client._cached_user_agent = None
    yield
    weather_client._cached_user_agent = None


def _install_transport(monkeypatch, responses):
    transport = RecordingTransport(responses)
    monkeypatch.setattr(weather_client._session, "request", transport)
    return transport


# ---------------------------------------------------------------------------
# resolve_location
# ---------------------------------------------------------------------------

def test_resolve_location_parses_a_successful_geocoding_match(monkeypatch):
    payload = {
        "results": [
            {
                "id": 1, "name": "Chicago", "latitude": 41.85, "longitude": -87.65,
                "country": "United States", "country_code": "US", "admin1": "Illinois",
                "timezone": "America/Chicago", "population": 2746388,
            },
            {
                "id": 2, "name": "Chicago", "latitude": 41.60, "longitude": -87.30,
                "country": "United States", "country_code": "US", "admin1": "Illinois",
                "timezone": "America/Chicago", "population": 500,
            },
            {
                "id": 3, "name": "Chicago Heights", "latitude": 41.51, "longitude": -87.63,
                "country": "United States", "country_code": "US", "admin1": "Illinois",
                "timezone": "America/Chicago", "population": 30276,
            },
        ]
    }
    transport = _install_transport(monkeypatch, [FakeResponse(200, payload)])

    place = weather_client.resolve_location("Chicago")

    assert place == {
        "name": "Chicago",
        "admin1": "Illinois",
        "country": "United States",
        "country_code": "US",
        "latitude": 41.85,
        "longitude": -87.65,
        "timezone": "America/Chicago",
        "label": "Chicago, Illinois, United States",
        "source": "open-meteo-geocoding",
        "alternatives": [
            "Chicago, Illinois, United States",
            "Chicago Heights, Illinois, United States",
        ],
    }
    assert transport.call_count == 1
    assert transport.calls[0]["params"]["name"] == "Chicago"


def test_resolve_location_coordinate_fast_path_makes_no_http_call(monkeypatch):
    transport = _install_transport(monkeypatch, [])  # any call pops an empty list and errors

    place = weather_client.resolve_location("41.88,-87.63")

    assert place == {
        "name": "41.88, -87.63",
        "admin1": None,
        "country": None,
        "country_code": None,
        "latitude": 41.88,
        "longitude": -87.63,
        "timezone": "auto",
        "label": "41.88, -87.63",
        "source": "coordinates",
        "alternatives": [],
    }
    assert transport.call_count == 0


def test_resolve_location_coordinate_fast_path_tolerates_whitespace(monkeypatch):
    _install_transport(monkeypatch, [])
    place = weather_client.resolve_location("  41.88 , -87.63 ")
    assert place["latitude"] == 41.88
    assert place["longitude"] == -87.63


@pytest.mark.parametrize("query", ["", "   ", None])
def test_resolve_location_blank_query_is_invalid_request(query):
    with pytest.raises(InvalidRequestError):
        weather_client.resolve_location(query)


def test_resolve_location_empty_results_is_location_not_found(monkeypatch):
    _install_transport(monkeypatch, [FakeResponse(200, {"results": []})])
    with pytest.raises(LocationNotFoundError):
        weather_client.resolve_location("Nowhereville")


# ---------------------------------------------------------------------------
# current_conditions
# ---------------------------------------------------------------------------

def test_current_conditions_parses_metric_and_imperial_fields(monkeypatch):
    payload = {
        "timezone": "America/Chicago",
        "current": {
            "time": "2026-08-09T14:00",
            "temperature_2m": 27.4,
            "relative_humidity_2m": 63,
            "apparent_temperature": 29.1,
            "is_day": 1,
            "precipitation": 0.0,
            "weather_code": 2,
            "wind_speed_10m": 14.8,
            "wind_direction_10m": 202,
            "surface_pressure": 1012.3,
        },
    }
    _install_transport(monkeypatch, [FakeResponse(200, payload)])

    result = weather_client.current_conditions(_place())

    assert result["observed_at"] == "2026-08-09T14:00"
    assert result["timezone"] == "America/Chicago"
    assert result["temperature_c"] == 27.4
    assert result["temperature_f"] == 81.3
    assert result["feels_like_c"] == 29.1
    assert result["feels_like_f"] == 84.4
    assert result["humidity_pct"] == 63
    assert result["precipitation_mm"] == 0.0
    assert result["precipitation_in"] == 0.0
    assert result["wind_speed_kmh"] == 14.8
    assert result["wind_speed_mph"] == 9.2
    assert result["wind_direction_deg"] == 202
    assert result["wind_direction_compass"] == "SSW"
    assert result["pressure_hpa"] == 1012.3
    assert result["is_day"] is True
    assert result["weather_code"] == 2
    assert result["conditions"] == "Partly cloudy"


@pytest.mark.parametrize(
    "deg, expected",
    [(0, "N"), (90, "E"), (180, "S"), (270, "W"), (202, "SSW")],
)
def test_compass_conversion_boundaries(deg, expected):
    assert weather_client._compass(deg) == expected


# ---------------------------------------------------------------------------
# daily_forecast
# ---------------------------------------------------------------------------

def _daily_payload():
    return {
        "timezone": "America/Chicago",
        "daily": {
            "time": ["2026-08-10", "2026-08-11"],
            "weather_code": [61, None],
            "temperature_2m_max": [28.0, None],
            "temperature_2m_min": [18.0, 17.0],
            "apparent_temperature_max": [29.0, None],
            "precipitation_sum": [3.2, None],
            "precipitation_probability_max": [55, None],
            "wind_speed_10m_max": [20.0, None],
            "wind_gusts_10m_max": [35.0, None],
            "uv_index_max": [6.2, None],
            "sunrise": ["2026-08-10T05:58", "2026-08-11T05:59"],
            "sunset": ["2026-08-10T20:07", "2026-08-11T20:06"],
        },
    }


def test_daily_forecast_parses_metric_and_imperial_fields(monkeypatch):
    _install_transport(monkeypatch, [FakeResponse(200, _daily_payload())])

    forecast = weather_client.daily_forecast(_place(), 2)
    day0 = forecast["days"][0]

    assert day0["date"] == "2026-08-10"
    assert day0["weather_code"] == 61
    assert day0["conditions"] == "Slight rain"
    assert day0["temp_high_c"] == 28.0
    assert day0["temp_high_f"] == 82.4
    assert day0["temp_low_c"] == 18.0
    assert day0["temp_low_f"] == 64.4
    assert day0["precipitation_mm"] == 3.2
    assert day0["precipitation_chance_pct"] == 55
    assert day0["wind_max_kmh"] == 20.0
    assert day0["wind_gust_max_kmh"] == 35.0
    assert day0["uv_index_max"] == 6.2
    assert day0["sunrise"] == "2026-08-10T05:58"
    assert day0["sunset"] == "2026-08-10T20:07"


def test_daily_forecast_tolerates_null_entries_without_raising(monkeypatch):
    _install_transport(monkeypatch, [FakeResponse(200, _daily_payload())])

    forecast = weather_client.daily_forecast(_place(), 2)
    day1 = forecast["days"][1]

    assert day1["date"] == "2026-08-11"
    assert day1["weather_code"] is None
    assert day1["conditions"] == "Unknown"
    assert day1["temp_high_c"] is None
    assert day1["temp_high_f"] is None
    assert day1["temp_low_c"] == 17.0
    assert day1["precipitation_mm"] is None
    assert day1["precipitation_chance_pct"] is None
    assert day1["wind_max_kmh"] is None
    assert day1["uv_index_max"] is None


@pytest.mark.parametrize("days", [1, 16])
def test_daily_forecast_accepts_boundary_day_counts(monkeypatch, days):
    transport = _install_transport(monkeypatch, [FakeResponse(200, {"timezone": "auto", "daily": {"time": []}})])
    weather_client.daily_forecast(_place(), days)
    assert transport.calls[0]["params"]["forecast_days"] == days


@pytest.mark.parametrize("days", [0, 17, -1])
def test_daily_forecast_rejects_out_of_range_day_counts(monkeypatch, days):
    transport = _install_transport(monkeypatch, [])
    with pytest.raises(InvalidRequestError):
        weather_client.daily_forecast(_place(), days)
    assert transport.call_count == 0


# ---------------------------------------------------------------------------
# historical_daily - date validation
# ---------------------------------------------------------------------------

def test_historical_daily_rejects_start_after_end(monkeypatch):
    transport = _install_transport(monkeypatch, [])
    with pytest.raises(InvalidRequestError):
        weather_client.historical_daily(_place(), "2024-01-10", "2024-01-05")
    assert transport.call_count == 0


def test_historical_daily_rejects_end_date_too_recent_for_archive_lag(monkeypatch):
    transport = _install_transport(monkeypatch, [])
    too_recent_end = (date.today() - timedelta(days=4)).isoformat()
    start = (date.today() - timedelta(days=10)).isoformat()
    with pytest.raises(InvalidRequestError):
        weather_client.historical_daily(_place(), start, too_recent_end)
    assert transport.call_count == 0


def test_historical_daily_accepts_end_date_exactly_at_archive_lag(monkeypatch):
    end = (date.today() - timedelta(days=5)).isoformat()
    start = (date.today() - timedelta(days=10)).isoformat()
    transport = _install_transport(monkeypatch, [FakeResponse(200, {"timezone": "auto", "daily": {"time": []}})])
    weather_client.historical_daily(_place(), start, end)
    assert transport.call_count == 1


def test_historical_daily_rejects_span_over_366_days(monkeypatch):
    transport = _install_transport(monkeypatch, [])
    end = (date.today() - timedelta(days=6)).isoformat()
    start = (date.today() - timedelta(days=400)).isoformat()
    with pytest.raises(InvalidRequestError):
        weather_client.historical_daily(_place(), start, end)
    assert transport.call_count == 0


def test_historical_daily_rejects_malformed_date_string(monkeypatch):
    transport = _install_transport(monkeypatch, [])
    with pytest.raises(InvalidRequestError):
        weather_client.historical_daily(_place(), "not-a-date", "2024-01-01")
    assert transport.call_count == 0


# ---------------------------------------------------------------------------
# retry behaviour
# ---------------------------------------------------------------------------

def test_a_500_then_a_200_succeeds_and_calls_twice(monkeypatch):
    payload = {"results": [{"id": 1, "name": "Chicago", "latitude": 41.85, "longitude": -87.65, "country": "United States", "country_code": "US", "admin1": "Illinois", "timezone": "America/Chicago", "population": 100}]}
    transport = _install_transport(monkeypatch, [FakeResponse(500), FakeResponse(200, payload)])

    place = weather_client.resolve_location("Chicago")

    assert place["name"] == "Chicago"
    assert transport.call_count == 2


def test_two_500s_raise_upstream_api_error(monkeypatch):
    transport = _install_transport(monkeypatch, [FakeResponse(500), FakeResponse(500)])
    with pytest.raises(UpstreamAPIError):
        weather_client.resolve_location("Chicago")
    assert transport.call_count == 2


def test_a_404_is_an_invalid_request_not_an_upstream_error(monkeypatch):
    """F4: a 4xx other than 429 is the caller's problem (a bad argument we
    passed upstream), not the service being down, so it is not retried and
    is not classified as upstream_error."""
    transport = _install_transport(monkeypatch, [FakeResponse(404)])
    with pytest.raises(InvalidRequestError):
        weather_client.resolve_location("Chicago")
    assert transport.call_count == 1


def test_a_4xx_with_a_reason_body_preserves_the_upstreams_explanation(monkeypatch):
    """F4: confirmed case - get_historical_weather("Chicago", "1930-01-01", "1930-01-05")
    got back a 400 whose body explained the archive's actual valid range,
    and the code used to throw that explanation away."""
    body = {"reason": "Parameter 'start_date' is out of allowed range from 1940-01-01 to 2026-08-09"}
    transport = _install_transport(monkeypatch, [FakeResponse(400, body)])
    with pytest.raises(InvalidRequestError, match="1940-01-01"):
        weather_client._get_json("https://example.invalid", params={}, upstream="open-meteo-archive")
    assert transport.call_count == 1


def test_a_429_is_still_retried_and_classified_as_upstream_error(monkeypatch):
    """F4 carves 429 out of the new 4xx-is-invalid_request rule; it stays on the retryable path."""
    transport = _install_transport(monkeypatch, [FakeResponse(429), FakeResponse(429)])
    with pytest.raises(UpstreamAPIError):
        weather_client.resolve_location("Chicago")
    assert transport.call_count == 2


def test_connection_error_retries_then_raises_upstream_api_error(monkeypatch):
    import requests

    transport = _install_transport(
        monkeypatch, [requests.ConnectionError("refused"), requests.ConnectionError("refused")]
    )
    with pytest.raises(UpstreamAPIError):
        weather_client.resolve_location("Chicago")
    assert transport.call_count == 2


# ---------------------------------------------------------------------------
# NWS alerts
# ---------------------------------------------------------------------------

def test_active_alerts_400_out_of_bounds_gives_unsupported_region(monkeypatch):
    """F1: confirmed live behaviour - NWS answers a non-US point (e.g. Paris)
    with HTTP 400 and detail 'out of bounds', not a 404. This is the primary
    no-coverage case; see the 404 test below for the secondary one."""
    body = {"detail": 'Parameter "point" is invalid: out of bounds'}
    _install_transport(monkeypatch, [FakeResponse(400, body)])
    result = weather_client.active_alerts(_place(country_code=None, latitude=48.85, longitude=2.35))
    assert result["coverage"] == "unsupported_region"
    assert result["alerts"] == []
    assert result["count"] == 0


def test_active_alerts_400_with_an_unrelated_detail_still_raises(monkeypatch):
    """F1: only a 400 whose body actually says 'out of bounds' means no coverage;
    any other 400 body is a genuinely malformed request."""
    body = {"detail": "Some other validation problem"}
    _install_transport(monkeypatch, [FakeResponse(400, body)])
    with pytest.raises(UpstreamAPIError):
        weather_client.active_alerts(_place(country_code=None))


def test_active_alerts_404_also_gives_unsupported_region(monkeypatch):
    """Secondary no-coverage case: some points still 404 rather than 400."""
    _install_transport(monkeypatch, [FakeResponse(404)])
    result = weather_client.active_alerts(_place(country_code="BR"))
    assert result == {"location": _place(country_code="BR"), "coverage": "unsupported_region", "alerts": [], "count": 0}


def test_active_alerts_parses_features_and_truncates_long_description(monkeypatch):
    long_description = "x" * 650
    long_instruction = "y" * 650
    payload = {
        "features": [
            {
                "properties": {
                    "event": "Severe Thunderstorm Warning",
                    "severity": "Severe",
                    "urgency": "Immediate",
                    "certainty": "Observed",
                    "headline": "Severe Thunderstorm Warning issued",
                    "areaDesc": "Cook County",
                    "onset": "2026-08-09T13:00:00-05:00",
                    "ends": "2026-08-09T14:00:00-05:00",
                    "expires": "2026-08-09T15:00:00-05:00",
                    "instruction": long_instruction,
                    "description": long_description,
                    "senderName": "NWS Chicago",
                }
            }
        ]
    }
    _install_transport(monkeypatch, [FakeResponse(200, payload)])

    result = weather_client.active_alerts(_place(country_code="US"))

    assert result["coverage"] == "us_nws"
    assert result["count"] == 1
    alert = result["alerts"][0]
    assert alert["event"] == "Severe Thunderstorm Warning"
    assert alert["severity"] == "Severe"
    assert alert["area"] == "Cook County"
    assert alert["sender"] == "NWS Chicago"
    assert alert["ends"] == "2026-08-09T14:00:00-05:00"
    assert alert["expires"] == "2026-08-09T15:00:00-05:00"
    assert len(alert["description"]) == 601
    assert alert["description"].endswith("…")
    assert alert["description"][:600] == long_description[:600]
    assert alert["instruction"] == long_instruction  # kept whole, never truncated


def test_active_alerts_empty_200_for_us_point_is_supported_with_no_alerts(monkeypatch):
    _install_transport(monkeypatch, [FakeResponse(200, {"features": []})])
    result = weather_client.active_alerts(_place(country_code="US"))
    assert result["coverage"] == "us_nws"
    assert result["alerts"] == []


def test_active_alerts_empty_200_for_non_us_country_code_is_still_covered(monkeypatch):
    """F2: coverage is a pure function of the HTTP response now, not a country-code
    heuristic. This used to assert 'unsupported_region' here, which was the bug:
    US territories (Puerto Rico, Guam, ...) get a non-"US" country_code from
    Open-Meteo but are actively served by NWS, so this empty 200 means covered
    with nothing active, the same as it would for country_code="US"."""
    _install_transport(monkeypatch, [FakeResponse(200, {"features": []})])
    result = weather_client.active_alerts(_place(country_code="BR"))
    assert result["coverage"] == "us_nws"
    assert result["alerts"] == []


def test_active_alerts_empty_200_for_unknown_country_is_treated_as_supported(monkeypatch):
    """A coordinate-resolved place has country_code=None; coverage does not
    look at country_code at all (F2), so this is 'us_nws' the same as any
    other empty-but-200 response."""
    _install_transport(monkeypatch, [FakeResponse(200, {"features": []})])
    result = weather_client.active_alerts(_place(country_code=None))
    assert result["coverage"] == "us_nws"
    assert result["alerts"] == []


def test_active_alerts_sends_user_agent_header(monkeypatch):
    # APP_NAME is read once at import time, not per-call, so assert against
    # the module's actual value instead of re-setting an env var that would
    # not be re-read. NWS_CONTACT_EMAIL is read lazily by _contact_email(),
    # so that one can be overridden here.
    monkeypatch.setenv("NWS_CONTACT_EMAIL", "test@example.com")
    monkeypatch.delenv("NWS_CONTACT_SECRET_SCOPE", raising=False)
    monkeypatch.delenv("NWS_CONTACT_SECRET_KEY", raising=False)
    transport = _install_transport(monkeypatch, [FakeResponse(200, {"features": []})])

    weather_client.active_alerts(_place(country_code="US"))

    headers = transport.calls[0]["headers"]
    assert headers["User-Agent"] == f"{weather_client.APP_NAME} (test@example.com)"


# ---------------------------------------------------------------------------
# forecast_day_for_date (F3)
# ---------------------------------------------------------------------------

def _window_payload(start="2026-08-10", count=16):
    """
    A 16-day daily forecast payload starting on `start`, one day apart.
    `start` is deliberately not "today" from the test's point of view, to
    prove day matching goes off the dates Open-Meteo actually returned,
    never off arithmetic against the server's clock.
    """
    start_date = date.fromisoformat(start)
    dates = [(start_date + timedelta(days=i)).isoformat() for i in range(count)]
    daily = {
        "time": dates,
        "weather_code": [1] * count,
        "temperature_2m_max": [20.0] * count,
        "temperature_2m_min": [10.0] * count,
        "apparent_temperature_max": [21.0] * count,
        "precipitation_sum": [0.0] * count,
        "precipitation_probability_max": [5] * count,
        "wind_speed_10m_max": [10.0] * count,
        "wind_gusts_10m_max": [15.0] * count,
        "uv_index_max": [3.0] * count,
        "sunrise": [f"{d}T06:00" for d in dates],
        "sunset": [f"{d}T20:00" for d in dates],
    }
    return {"timezone": "Pacific/Auckland", "daily": daily}


def test_forecast_day_for_date_none_returns_the_first_day_in_the_window(monkeypatch):
    """The Auckland case: the API window starts 2026-08-10, which the test's
    "today" is well behind. timezone=auto defines day 0 as today in the
    location's own zone, whatever the server's clock says."""
    _install_transport(monkeypatch, [FakeResponse(200, _window_payload(start="2026-08-10"))])
    day, resolved_date = weather_client.forecast_day_for_date(_place(), None)
    assert resolved_date == "2026-08-10"
    assert day["date"] == "2026-08-10"


def test_forecast_day_for_date_matches_on_the_dates_the_api_returned(monkeypatch):
    _install_transport(monkeypatch, [FakeResponse(200, _window_payload(start="2026-08-10"))])
    day, resolved_date = weather_client.forecast_day_for_date(_place(), "2026-08-15")
    assert resolved_date == "2026-08-15"
    assert day["date"] == "2026-08-15"


def test_forecast_day_for_date_accepts_the_windows_last_real_day(monkeypatch):
    """Confirmed failure: Auckland's last real day, 2026-08-25, used to be
    rejected as out of range by the old server-relative arithmetic."""
    _install_transport(monkeypatch, [FakeResponse(200, _window_payload(start="2026-08-10", count=16))])
    day, resolved_date = weather_client.forecast_day_for_date(_place(), "2026-08-25")
    assert resolved_date == "2026-08-25"
    assert day["date"] == "2026-08-25"


def test_forecast_day_for_date_out_of_window_names_the_real_window(monkeypatch):
    _install_transport(monkeypatch, [FakeResponse(200, _window_payload(start="2026-08-10", count=16))])
    with pytest.raises(InvalidRequestError) as exc_info:
        weather_client.forecast_day_for_date(_place(), "2026-08-26")
    message = str(exc_info.value)
    assert "2026-08-10" in message
    assert "2026-08-25" in message


def test_forecast_day_for_date_rejects_a_malformed_date(monkeypatch):
    _install_transport(monkeypatch, [FakeResponse(200, _window_payload())])
    with pytest.raises(InvalidRequestError):
        weather_client.forecast_day_for_date(_place(), "not-a-date")


# ---------------------------------------------------------------------------
# filter_alerts_for_date (F5)
# ---------------------------------------------------------------------------

def test_filter_alerts_for_date_drops_an_alert_that_already_ended():
    """Confirmed case: two Guam advisories ending 2026-08-13 docked 15 points
    from a 2026-08-23 score. They must not even reach travel_score."""
    alerts = [{"event": "Flood Advisory", "ends": "2026-08-13T18:00:00-04:00", "expires": None}]
    assert weather_client.filter_alerts_for_date(alerts, "2026-08-23") == []


def test_filter_alerts_for_date_keeps_an_alert_still_active_that_day():
    alerts = [{"event": "Flood Advisory", "ends": "2026-08-23T18:00:00-04:00", "expires": None}]
    assert weather_client.filter_alerts_for_date(alerts, "2026-08-23") == alerts


def test_filter_alerts_for_date_keeps_an_alert_with_no_stated_end():
    alerts = [{"event": "Flood Advisory", "ends": None, "expires": None}]
    assert weather_client.filter_alerts_for_date(alerts, "2026-08-23") == alerts


def test_filter_alerts_for_date_keeps_an_alert_with_a_malformed_timestamp():
    alerts = [{"event": "Flood Advisory", "ends": "not-a-timestamp", "expires": None}]
    assert weather_client.filter_alerts_for_date(alerts, "2026-08-23") == alerts  # must not raise


def test_filter_alerts_for_date_uses_expires_when_ends_is_absent():
    alerts = [{"event": "Flood Advisory", "ends": None, "expires": "2026-08-13T18:00:00-04:00"}]
    assert weather_client.filter_alerts_for_date(alerts, "2026-08-23") == []


# ---------------------------------------------------------------------------
# deadline budget (F11)
# ---------------------------------------------------------------------------

def test_request_with_retry_raises_immediately_once_the_deadline_has_passed(monkeypatch):
    """A spent deadline stops the retry loop before sleeping or trying
    again, on the status-retry path."""
    transport = _install_transport(monkeypatch, [FakeResponse(500)])
    past_deadline = weather_client.time.monotonic() - 1  # already expired
    with pytest.raises(UpstreamAPIError):
        weather_client._request_with_retry(
            "GET", "https://example.invalid", upstream="test-upstream", deadline=past_deadline
        )
    assert transport.call_count == 1  # no second attempt was made


def test_request_with_retry_raises_immediately_past_a_spent_deadline_on_connection_error(monkeypatch):
    """Same, on the connection-error path."""
    import requests

    transport = _install_transport(monkeypatch, [requests.ConnectionError("refused")])
    past_deadline = weather_client.time.monotonic() - 1
    with pytest.raises(UpstreamAPIError):
        weather_client._request_with_retry(
            "GET", "https://example.invalid", upstream="test-upstream", deadline=past_deadline
        )
    assert transport.call_count == 1


def test_new_deadline_is_the_default_budget_from_now():
    before = weather_client.time.monotonic()
    deadline = weather_client.new_deadline()
    after = weather_client.time.monotonic()
    assert before + weather_client.DEFAULT_UPSTREAM_BUDGET_S <= deadline <= after + weather_client.DEFAULT_UPSTREAM_BUDGET_S


# ---------------------------------------------------------------------------
# other requests.RequestException subclasses (F12)
# ---------------------------------------------------------------------------

def test_an_unretried_request_exception_is_classified_as_upstream_error(monkeypatch):
    """ChunkedEncodingError, TooManyRedirects, ContentDecodingError, etc. are
    requests.RequestException but not one of the two specifically retried
    errors; they must not escape as an untyped exception."""
    import requests

    transport = _install_transport(monkeypatch, [requests.exceptions.ChunkedEncodingError("truncated")])
    with pytest.raises(UpstreamAPIError):
        weather_client.resolve_location("Chicago")
    assert transport.call_count == 1  # not retried
