"""
Tests for recommendations.py: umbrella_advice, packing_list, travel_score,
and compare_days. This is pure logic (no I/O), so every test here builds
plain day dicts by hand and checks the returned dict.
"""

import pytest

import recommendations
from recommendations import (
    SNOW_WEATHER_CODES,
    compare_days,
    packing_list,
    travel_score,
    umbrella_advice,
)


def _day(**overrides):
    """A forecast day with no umbrella/travel signal at all, for isolating one field at a time."""
    base = {
        "precipitation_chance_pct": 0,
        "precipitation_mm": 0.0,
        "wind_max_kmh": 0.0,
        "weather_code": 0,
        "temp_high_c": 20.0,
        "temp_low_c": 10.0,
        "uv_index_max": 3.0,
    }
    base.update(overrides)
    return base


def _all_none_day():
    return {
        "precipitation_chance_pct": None,
        "precipitation_mm": None,
        "wind_max_kmh": None,
        "weather_code": None,
        "temp_high_c": None,
        "temp_low_c": None,
        "uv_index_max": None,
    }


# ---------------------------------------------------------------------------
# umbrella_advice - threshold table, percentage path
# ---------------------------------------------------------------------------

def test_umbrella_chance_exactly_60_is_yes_high():
    advice = umbrella_advice(_day(precipitation_chance_pct=60))
    assert advice["verdict"] == "yes"
    assert advice["confidence"] == "high"
    assert advice["rule_fired"] == "chance>=60_or_mm>=5.0"


def test_umbrella_chance_59_9_drops_to_yes_medium():
    advice = umbrella_advice(_day(precipitation_chance_pct=59.9))
    assert advice["verdict"] == "yes"
    assert advice["confidence"] == "medium"
    assert advice["rule_fired"] == "chance>=40_or_mm>=1.0"


def test_umbrella_chance_exactly_40_is_yes_medium():
    advice = umbrella_advice(_day(precipitation_chance_pct=40))
    assert advice["verdict"] == "yes"
    assert advice["confidence"] == "medium"
    assert advice["rule_fired"] == "chance>=40_or_mm>=1.0"


def test_umbrella_chance_39_9_drops_to_maybe_medium():
    advice = umbrella_advice(_day(precipitation_chance_pct=39.9))
    assert advice["verdict"] == "maybe"
    assert advice["confidence"] == "medium"
    assert advice["rule_fired"] == "chance>=20_or_mm>=0.2"


def test_umbrella_chance_exactly_20_is_maybe_medium():
    advice = umbrella_advice(_day(precipitation_chance_pct=20))
    assert advice["verdict"] == "maybe"
    assert advice["confidence"] == "medium"
    assert advice["rule_fired"] == "chance>=20_or_mm>=0.2"


def test_umbrella_says_no_when_chance_below_twenty():
    advice = umbrella_advice(_day(precipitation_chance_pct=19.9))
    assert advice["verdict"] == "no"
    assert advice["confidence"] == "high"
    assert advice["rule_fired"] == "none"


def test_umbrella_chance_exactly_zero_is_no_high():
    advice = umbrella_advice(_day(precipitation_chance_pct=0))
    assert advice["verdict"] == "no"
    assert advice["confidence"] == "high"
    assert advice["rule_fired"] == "none"


# ---------------------------------------------------------------------------
# umbrella_advice - threshold table, millimetre path (chance held at 0)
# ---------------------------------------------------------------------------

def test_umbrella_mm_exactly_5_is_yes_high():
    advice = umbrella_advice(_day(precipitation_mm=5.0))
    assert advice["verdict"] == "yes"
    assert advice["confidence"] == "high"
    assert advice["rule_fired"] == "chance>=60_or_mm>=5.0"


def test_umbrella_mm_4_9_drops_to_yes_medium():
    advice = umbrella_advice(_day(precipitation_mm=4.9))
    assert advice["verdict"] == "yes"
    assert advice["confidence"] == "medium"
    assert advice["rule_fired"] == "chance>=40_or_mm>=1.0"


def test_umbrella_mm_exactly_1_is_yes_medium():
    advice = umbrella_advice(_day(precipitation_mm=1.0))
    assert advice["verdict"] == "yes"
    assert advice["confidence"] == "medium"
    assert advice["rule_fired"] == "chance>=40_or_mm>=1.0"


def test_umbrella_mm_0_99_drops_to_maybe_medium():
    advice = umbrella_advice(_day(precipitation_mm=0.99))
    assert advice["verdict"] == "maybe"
    assert advice["confidence"] == "medium"
    assert advice["rule_fired"] == "chance>=20_or_mm>=0.2"


def test_umbrella_mm_exactly_0_2_is_maybe_medium():
    advice = umbrella_advice(_day(precipitation_mm=0.2))
    assert advice["verdict"] == "maybe"
    assert advice["confidence"] == "medium"
    assert advice["rule_fired"] == "chance>=20_or_mm>=0.2"


def test_umbrella_mm_0_19_is_no_high():
    advice = umbrella_advice(_day(precipitation_mm=0.19))
    assert advice["verdict"] == "no"
    assert advice["confidence"] == "high"
    assert advice["rule_fired"] == "none"


def test_umbrella_mm_exactly_zero_is_no_high():
    advice = umbrella_advice(_day(precipitation_mm=0.0))
    assert advice["verdict"] == "no"
    assert advice["confidence"] == "high"
    assert advice["rule_fired"] == "none"


# ---------------------------------------------------------------------------
# umbrella_advice - snow override
# ---------------------------------------------------------------------------

def test_umbrella_snow_codes_cover_the_full_set():
    assert SNOW_WEATHER_CODES == {71, 73, 75, 77, 85, 86}


def test_umbrella_every_snow_code_forces_no_and_boots_reason():
    for code in SNOW_WEATHER_CODES:
        advice = umbrella_advice(_day(weather_code=code))
        assert advice["verdict"] == "no", code
        assert advice["rule_fired"] == "snow_override", code
        assert "coat" in advice["reason"] or "hood" in advice["reason"]


def test_umbrella_snow_override_wins_over_high_rain_chance():
    advice = umbrella_advice(_day(precipitation_chance_pct=90, precipitation_mm=10.0, weather_code=71))
    assert advice["verdict"] == "no"
    assert advice["rule_fired"] == "snow_override"


# ---------------------------------------------------------------------------
# umbrella_advice - wind warning override
# ---------------------------------------------------------------------------

def test_umbrella_wind_warning_fires_at_40_kmh_when_yes():
    advice = umbrella_advice(_day(precipitation_chance_pct=60, wind_max_kmh=40))
    assert advice["verdict"] == "yes"
    assert advice["wind_warning"] is True


def test_umbrella_wind_warning_does_not_fire_at_39_kmh():
    advice = umbrella_advice(_day(precipitation_chance_pct=60, wind_max_kmh=39))
    assert advice["verdict"] == "yes"
    assert advice["wind_warning"] is False


def test_umbrella_wind_warning_does_not_fire_when_verdict_is_no():
    advice = umbrella_advice(_day(precipitation_chance_pct=0, precipitation_mm=0.0, wind_max_kmh=40))
    assert advice["verdict"] == "no"
    assert advice["wind_warning"] is False


def test_umbrella_wind_warning_fires_for_maybe_too():
    advice = umbrella_advice(_day(precipitation_chance_pct=25, wind_max_kmh=40))
    assert advice["verdict"] == "maybe"
    assert advice["wind_warning"] is True


# ---------------------------------------------------------------------------
# umbrella_advice - None precipitation_chance_pct caveat
# ---------------------------------------------------------------------------

def test_umbrella_none_chance_drops_high_to_medium():
    advice = umbrella_advice(_day(precipitation_chance_pct=None, precipitation_mm=0.0))
    assert advice["verdict"] == "no"
    assert advice["confidence"] == "medium"
    assert "not reported" in advice["reason"]
    assert advice["inputs_used"]["precipitation_chance_pct"] is None


def test_umbrella_none_chance_drops_medium_to_low():
    advice = umbrella_advice(_day(precipitation_chance_pct=None, precipitation_mm=1.0))
    assert advice["verdict"] == "yes"
    assert advice["confidence"] == "low"


def test_lower_confidence_does_not_underflow_past_low():
    assert recommendations._lower_confidence("low") == "low"
    assert recommendations._lower_confidence("high") == "medium"
    assert recommendations._lower_confidence("medium") == "low"


# ---------------------------------------------------------------------------
# umbrella_advice - reason cites only the side that actually fired (F7)
# ---------------------------------------------------------------------------

def test_umbrella_reason_does_not_cite_a_chance_that_never_crossed_its_threshold():
    """Confirmed bad case: chance 3%, 9.0 mm used to read 'Precipitation chance
    is 3% and expected precipitation is 9.0 mm, crossing the ... threshold.' -
    a contradiction, since 3% never fired anything. Only mm fired here."""
    advice = umbrella_advice(_day(precipitation_chance_pct=3, precipitation_mm=9.0))
    assert advice["verdict"] == "yes"
    assert advice["rule_fired"] == "chance>=60_or_mm>=5.0"
    assert "3%" not in advice["reason"]
    assert "9.0 mm" in advice["reason"]
    # rule_fired stays the machine-readable id (untouched by F7) but must
    # not leak into the human sentence.
    assert advice["rule_fired"] not in advice["reason"]


def test_umbrella_reason_never_invents_a_zero_percent_for_a_missing_chance():
    """Confirmed bad case: chance None, 1.5 mm used to read 'Precipitation
    chance is 0% ...', inventing a figure inputs_used correctly reports as None."""
    advice = umbrella_advice(_day(precipitation_chance_pct=None, precipitation_mm=1.5))
    assert advice["verdict"] == "yes"
    assert "0%" not in advice["reason"]
    assert "not reported" in advice["reason"]
    assert "1.5 mm" in advice["reason"]
    assert advice["inputs_used"]["precipitation_chance_pct"] is None


def test_umbrella_reason_cites_both_when_both_sides_fire():
    advice = umbrella_advice(_day(precipitation_chance_pct=70, precipitation_mm=6.0))
    assert "70%" in advice["reason"]
    assert "6.0 mm" in advice["reason"]


def test_umbrella_reason_does_not_claim_both_are_low_when_neither_was_reported():
    """Follow-up: umbrella_advice({}) used to say 'Precipitation chance and
    amount are both low. Precipitation chance was not reported, ...' - a
    contradiction, since an unreported amount is not known to be low
    either. Neither figure was reported, so the reason must say that
    plainly instead, and confidence must reflect having nothing to go on."""
    advice = umbrella_advice(_day(precipitation_chance_pct=None, precipitation_mm=None))
    assert advice["verdict"] == "no"
    assert advice["confidence"] == "low"
    assert "both low" not in advice["reason"]
    assert "neither" in advice["reason"].lower()
    assert advice["inputs_used"]["precipitation_chance_pct"] is None
    assert advice["inputs_used"]["precipitation_mm"] is None


def test_umbrella_reason_still_reports_amount_as_low_when_only_chance_is_missing():
    """A single missing field is a different case (existing behaviour,
    untouched): precipitation_mm=0.0 is a real reported value, so "both
    low" still holds and only chance gets the missing-data caveat."""
    advice = umbrella_advice(_day(precipitation_chance_pct=None, precipitation_mm=0.0))
    assert "both low" in advice["reason"]
    assert advice["confidence"] == "medium"


# ---------------------------------------------------------------------------
# packing_list
# ---------------------------------------------------------------------------

def test_packing_list_heavy_coat_below_zero():
    items = packing_list(_day(temp_low_c=-1))
    assert "a heavy winter coat" in items
    assert "a jacket" not in items


def test_packing_list_jacket_between_zero_and_twelve():
    items = packing_list(_day(temp_low_c=5))
    assert "a jacket" in items
    assert "a heavy winter coat" not in items


def test_packing_list_no_jacket_at_exactly_twelve():
    items = packing_list(_day(temp_low_c=12))
    assert "a jacket" not in items
    assert "a heavy winter coat" not in items


def test_packing_list_umbrella_when_verdict_yes():
    items = packing_list(_day(precipitation_chance_pct=60, temp_low_c=15))
    assert "an umbrella" in items
    assert "waterproof boots" not in items


def test_packing_list_boots_on_snow_override_not_umbrella():
    items = packing_list(_day(precipitation_chance_pct=90, weather_code=71, temp_low_c=15))
    assert "waterproof boots" in items
    assert "an umbrella" not in items


def test_packing_list_sunscreen_at_uv_6():
    items = packing_list(_day(uv_index_max=6, temp_low_c=15))
    assert "sunscreen" in items


def test_packing_list_no_sunscreen_below_uv_6():
    items = packing_list(_day(uv_index_max=5.9, temp_low_c=15))
    assert "sunscreen" not in items


def test_packing_list_layers_when_swing_over_12():
    items = packing_list(_day(temp_high_c=25, temp_low_c=12.9))
    assert "layers, the day swings more than 12 C" in items


def test_packing_list_no_layers_when_swing_exactly_12():
    items = packing_list(_day(temp_high_c=24, temp_low_c=12))
    assert "layers, the day swings more than 12 C" not in items


def test_packing_list_windproof_layer_at_40_kmh():
    items = packing_list(_day(wind_max_kmh=40, temp_low_c=15))
    assert "a windproof outer layer" in items


def test_packing_list_no_windproof_layer_at_39_kmh():
    items = packing_list(_day(wind_max_kmh=39, temp_low_c=15))
    assert "a windproof outer layer" not in items


def test_packing_list_empty_for_a_mild_day():
    mild = _day(
        precipitation_chance_pct=0, precipitation_mm=0.0, wind_max_kmh=10,
        weather_code=0, temp_high_c=20, temp_low_c=15, uv_index_max=3,
    )
    assert packing_list(mild) == []


def test_packing_list_stable_ordering_across_all_branches():
    day = _day(
        temp_low_c=-5, temp_high_c=10, precipitation_chance_pct=60,
        uv_index_max=7, wind_max_kmh=45, weather_code=0,
    )
    assert packing_list(day) == [
        "a heavy winter coat",
        "an umbrella",
        "sunscreen",
        "layers, the day swings more than 12 C",
        "a windproof outer layer",
    ]


# ---------------------------------------------------------------------------
# travel_score - each penalty in isolation
# ---------------------------------------------------------------------------

def _baseline_day():
    """Every factor sits exactly at its threshold, so none of them fire and the score is 100."""
    return {
        "precipitation_chance_pct": 20,
        "precipitation_mm": 2.0,
        "wind_max_kmh": 30.0,
        "temp_high_c": 35.0,
        "temp_low_c": -5.0,
        "uv_index_max": 8.0,
    }


def test_travel_score_baseline_at_thresholds_is_100_no_factors():
    result = travel_score(_baseline_day())
    assert result["score"] == 100
    assert result["band"] == "good"
    assert result["factors"] == []


def test_travel_score_precipitation_chance_penalty_isolated():
    day = {**_baseline_day(), "precipitation_chance_pct": 70}
    result = travel_score(day)
    assert result["score"] == 80
    assert result["factors"] == [{"points": 20.0, "why": "Precipitation chance is 70%, above the 20% comfort threshold."}]


def test_travel_score_precipitation_mm_penalty_isolated():
    day = {**_baseline_day(), "precipitation_mm": 12.0}
    result = travel_score(day)
    assert result["score"] == 80
    assert result["factors"] == [{"points": 20.0, "why": "Precipitation is 12.0 mm, above the 2 mm comfort threshold."}]


def test_travel_score_wind_penalty_isolated():
    day = {**_baseline_day(), "wind_max_kmh": 60.0}
    result = travel_score(day)
    assert result["score"] == 70
    assert result["factors"] == [{"points": 30.0, "why": "Max wind is 60.0 km/h, above the 30 km/h comfort threshold."}]


def test_travel_score_high_temp_penalty_isolated():
    day = {**_baseline_day(), "temp_high_c": 45.0}
    result = travel_score(day)
    assert result["score"] == 70
    assert result["factors"] == [{"points": 30.0, "why": "High temperature is 45.0 C, above the 35 C comfort threshold."}]


def test_travel_score_low_temp_penalty_isolated():
    day = {**_baseline_day(), "temp_low_c": -15.0}
    result = travel_score(day)
    assert result["score"] == 70
    assert result["factors"] == [{"points": 30.0, "why": "Low temperature is -15.0 C, below the -5 C comfort threshold."}]


def test_travel_score_uv_penalty_is_flat_five():
    day = {**_baseline_day(), "uv_index_max": 9.0}
    result = travel_score(day)
    assert result["score"] == 95
    assert result["factors"] == [{"points": 5.0, "why": "UV index is 9.0, above 8."}]


def test_travel_score_alerts_severe_tier_is_40_points():
    result = travel_score(_baseline_day(), alerts=[{"severity": "Severe"}])
    assert result["factors"][-1]["points"] == 40.0
    assert result["alerts_considered"] == 1


def test_travel_score_alerts_extreme_tier_is_40_points():
    result = travel_score(_baseline_day(), alerts=[{"severity": "Extreme"}])
    assert result["factors"][-1]["points"] == 40.0


def test_travel_score_alerts_non_severe_tier_is_15_points():
    result = travel_score(_baseline_day(), alerts=[{"severity": "Minor"}])
    assert result["factors"][-1]["points"] == 15.0
    assert result["alerts_considered"] == 1


def test_travel_score_no_alerts_adds_no_factor():
    result = travel_score(_baseline_day(), alerts=[])
    assert result["alerts_considered"] == 0
    assert all("alert" not in f["why"] for f in result["factors"])


def test_travel_score_none_alerts_treated_like_no_alerts():
    result = travel_score(_baseline_day(), alerts=None)
    assert result["alerts_considered"] == 0


# ---------------------------------------------------------------------------
# travel_score - clamping and band boundaries
# ---------------------------------------------------------------------------

def test_travel_score_clamps_at_zero():
    result = travel_score({"wind_max_kmh": 1000.0})
    assert result["score"] == 0
    assert result["band"] == "poor"


def test_travel_score_empty_day_is_unknown_not_100():
    """F8: none of the six scoreable fields are present, so there is nothing
    to score - this must not read as a perfect 100/good day."""
    result = travel_score({})
    assert result["score"] is None
    assert result["band"] == "unknown"
    assert result["factors"] == []
    assert result["inputs_scored"] == 0
    assert result["inputs_available"] == 6


def test_travel_score_empty_day_score_is_none_not_zero():
    """Follow-up: score 0 is a real, meaningful (awful) value on this scale
    and must not double as "no data" - an agent reading score 0 would tell
    the user it's a terrible day to travel, when nothing is actually known."""
    result = travel_score({})
    assert result["score"] is None
    assert result["score"] != 0


def test_travel_score_reports_inputs_scored_and_available():
    day = {"precipitation_chance_pct": 10, "wind_max_kmh": 20.0}
    result = travel_score(day)
    assert result["inputs_scored"] == 2
    assert result["inputs_available"] == 6


def test_travel_score_full_day_is_not_unknown():
    result = travel_score(_baseline_day())
    assert result["band"] != "unknown"
    assert result["inputs_scored"] == 6


@pytest.mark.parametrize(
    "wind_kmh, expected_score, expected_band",
    [
        (50, 80, "good"),
        (51, 79, "fair"),
        (70, 60, "fair"),
        (71, 59, "marginal"),
        (90, 40, "marginal"),
        (91, 39, "poor"),
    ],
)
def test_travel_score_band_boundaries(wind_kmh, expected_score, expected_band):
    result = travel_score({"wind_max_kmh": wind_kmh})
    assert result["score"] == expected_score
    assert result["band"] == expected_band


def test_travel_score_headline_names_biggest_factor():
    day = {**_baseline_day(), "temp_high_c": 60.0, "wind_max_kmh": 31.0}
    result = travel_score(day)
    biggest = max(result["factors"], key=lambda f: f["points"])
    assert biggest["why"] in result["headline"]


def test_travel_score_includes_packing_list():
    day = _day(temp_low_c=-5)
    result = travel_score(day)
    assert result["packing_list"] == packing_list(day)


# ---------------------------------------------------------------------------
# compare_days
# ---------------------------------------------------------------------------

def test_compare_days_orders_descending_by_score():
    # A real, fully-scoreable "calm" day, not {} - travel_score({}) is now
    # "unknown"/0 (F8), which would make this test about empty-day scoring
    # instead of about ranking. See test_travel_score_empty_day_is_unknown_not_100
    # for that case.
    calm_day = {
        "precipitation_chance_pct": 0, "precipitation_mm": 0.0, "wind_max_kmh": 5.0,
        "temp_high_c": 22.0, "temp_low_c": 15.0, "uv_index_max": 3.0,
    }
    entries = [
        {"label": "Windy City", "day": {**calm_day, "wind_max_kmh": 90}, "alerts": None},
        {"label": "Calm Town", "day": calm_day, "alerts": None},
        {"label": "Breezy Ville", "day": {**calm_day, "wind_max_kmh": 50}, "alerts": None},
    ]
    result = compare_days(entries)
    labels = [r["label"] for r in result["ranked"]]
    assert labels == ["Calm Town", "Breezy Ville", "Windy City"]
    assert result["best"] == "Calm Town"
    assert result["worst"] == "Windy City"


def test_compare_days_tie_break_is_label_ascending():
    entries = [
        {"label": "Zebra", "day": {}, "alerts": None},
        {"label": "Alpha", "day": {}, "alerts": None},
    ]
    result = compare_days(entries)
    labels = [r["label"] for r in result["ranked"]]
    assert labels == ["Alpha", "Zebra"]
    assert result["best"] == "Alpha"
    assert result["worst"] == "Zebra"


def test_compare_days_tie_break_does_not_crash_on_a_none_label():
    """F9: key=lambda r: (-r["score"], r["label"]) raised TypeError comparing
    None to a str; label or "" fixes that without changing the score/order."""
    entries = [
        {"label": None, "day": {}, "alerts": None},
        {"label": "Alpha", "day": {}, "alerts": None},
    ]
    result = compare_days(entries)  # must not raise
    labels = [r["label"] for r in result["ranked"]]
    assert labels == [None, "Alpha"]


def test_compare_days_empty_entries_has_no_best_or_worst():
    result = compare_days([])
    assert result == {"ranked": [], "best": None, "worst": None}


def test_compare_days_single_entry_is_both_best_and_worst():
    result = compare_days([{"label": "Solo", "day": {}, "alerts": None}])
    assert result["best"] == "Solo"
    assert result["worst"] == "Solo"


def test_compare_days_a_no_data_entry_ranks_last_even_below_a_poor_score():
    """Follow-up ranking decision: an entry with score None (no scoreable
    data at all) always sorts after every entry with a real score, even a
    bad one - there is nothing to say it's better than a day that actually
    scored "poor", so it must never rank above one."""
    entries = [
        {"label": "NoData", "day": {}, "alerts": None},
        {"label": "AwfulDay", "day": {"wind_max_kmh": 1000.0}, "alerts": None},
    ]
    result = compare_days(entries)
    labels = [r["label"] for r in result["ranked"]]
    assert labels == ["AwfulDay", "NoData"]
    assert result["best"] == "AwfulDay"
    assert result["worst"] == "NoData"
    assert result["ranked"][0]["score"] == 0
    assert result["ranked"][1]["score"] is None


def test_compare_days_multiple_no_data_entries_tie_break_by_label():
    entries = [
        {"label": "Zebra", "day": {}, "alerts": None},
        {"label": "Alpha", "day": {}, "alerts": None},
        {"label": "RealDay", "day": {"wind_max_kmh": 10.0}, "alerts": None},
    ]
    result = compare_days(entries)
    labels = [r["label"] for r in result["ranked"]]
    assert labels == ["RealDay", "Alpha", "Zebra"]


# ---------------------------------------------------------------------------
# None tolerance across every function
# ---------------------------------------------------------------------------

def test_all_none_day_does_not_raise_from_any_function():
    day = _all_none_day()

    advice = umbrella_advice(day)
    assert advice["verdict"] in ("yes", "maybe", "no")

    items = packing_list(day)
    assert isinstance(items, list)

    score = travel_score(day, alerts=None)
    assert score["score"] is None or 0 <= score["score"] <= 100

    comparison = compare_days([{"label": "AllNone", "day": day, "alerts": None}])  # must not raise
    assert comparison["best"] == "AllNone"
