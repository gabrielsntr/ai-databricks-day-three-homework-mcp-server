"""
Derived-judgment logic for the weather MCP server: umbrella advice, a
0-100 travel score, packing lists, and multi-location comparison.

Every function here takes already-parsed dicts from weather_client (a day
dict from daily_forecast, an alerts list from active_alerts) and returns a
plain dict. Nothing in this module makes a network call, reads an
environment variable, or imports anything beyond the standard library, so
it is safe to unit test with plain literals and safe to call from anywhere
without side effects.

Any field on a day dict can be None (the upstream forecast omits it), so
every function here is written to treat a missing number as "no signal"
rather than raising.
"""

SNOW_WEATHER_CODES = {71, 73, 75, 77, 85, 86}

# The six day fields travel_score can actually penalise. Used to tell "a
# calm day" (every field present, nothing crossed a threshold) apart from
# "no forecast data at all" (every field absent), which used to both score
# a perfect 100.
SCOREABLE_FIELDS = (
    "precipitation_chance_pct", "precipitation_mm", "wind_max_kmh",
    "temp_high_c", "temp_low_c", "uv_index_max",
)

_CONFIDENCE_STEPS = ["high", "medium", "low"]

# Umbrella verdict/confidence thresholds, checked in order, first match wins.
# chance_threshold is a precipitation_chance_pct value, mm_threshold is a
# precipitation_mm value; either one crossing its threshold fires the rule.
UMBRELLA_RULES = [
    {"rule_fired": "chance>=60_or_mm>=5.0", "chance_threshold": 60, "mm_threshold": 5.0, "verdict": "yes", "confidence": "high"},
    {"rule_fired": "chance>=40_or_mm>=1.0", "chance_threshold": 40, "mm_threshold": 1.0, "verdict": "yes", "confidence": "medium"},
    {"rule_fired": "chance>=20_or_mm>=0.2", "chance_threshold": 20, "mm_threshold": 0.2, "verdict": "maybe", "confidence": "medium"},
]


def _lower_confidence(confidence: str) -> str:
    index = _CONFIDENCE_STEPS.index(confidence)
    return _CONFIDENCE_STEPS[min(index + 1, len(_CONFIDENCE_STEPS) - 1)]


def umbrella_advice(day: dict) -> dict:
    """
    Decide whether to bring an umbrella for a single forecast day.

    Reads precipitation_chance_pct, precipitation_mm, wind_max_kmh, and
    weather_code from `day`. precipitation_chance_pct may be None; when it
    is, it never counts toward a threshold (only precipitation_mm can fire
    a rule), and the reason says the chance was not reported instead of
    inventing a 0% figure.

    Base rule table (first match wins, by precipitation_chance_pct or
    precipitation_mm, whichever crosses its threshold first):
        chance >= 60 or precip >= 5.0 mm  -> verdict "yes", confidence "high"
        chance >= 40 or precip >= 1.0 mm  -> verdict "yes", confidence "medium"
        chance >= 20 or precip >= 0.2 mm  -> verdict "maybe", confidence "medium"
        otherwise                          -> verdict "no", confidence "high"

    The reason cites only whichever side(s) actually crossed the
    threshold that fired: the chance percentage, the millimetre amount, or
    both. It never names a value that had nothing to do with the verdict.

    Overrides applied after the table:
        - weather_code in the snow set (71, 73, 75, 77, 85, 86) forces
          verdict "no": an umbrella is the wrong tool for snow, the reason
          recommends a hood or waterproof coat instead.
        - wind_max_kmh >= 40 and the verdict is "yes" or "maybe" keeps the
          verdict but sets wind_warning True and notes a rain jacket beats
          an umbrella at that wind speed.
        - precipitation_chance_pct is None appends a caveat to the reason
          and drops confidence one step (high -> medium -> low).
        - precipitation_chance_pct and precipitation_mm are both None: the
          default reason says plainly that neither figure was reported
          (never "both low", which would assert low values the forecast
          did not report) and confidence goes straight to "low". Verdict
          stays "no", the sensible default with nothing to go on.

    Args:
        day: A forecast day dict as returned by weather_client.daily_forecast.

    Returns:
        A dict with verdict ("yes"/"maybe"/"no"), confidence ("high"/
        "medium"/"low"), reason, rule_fired, wind_warning, and
        inputs_used (the raw values this call reasoned over).
    """
    chance = day.get("precipitation_chance_pct")
    raw_mm = day.get("precipitation_mm")
    mm = raw_mm or 0.0
    wind_kmh = day.get("wind_max_kmh") or 0.0
    weather_code = day.get("weather_code")

    chance_missing = chance is None
    mm_missing = raw_mm is None
    chance_value = 0 if chance_missing else chance

    verdict = "no"
    confidence = "high"
    if chance_missing and mm_missing:
        # Neither figure was reported, so neither is known to be low -
        # asserting "both low" here would be a fact the data does not
        # support.
        reason = "Neither precipitation chance nor amount was reported for this day."
    else:
        reason = "Precipitation chance and amount are both low."
    rule_fired = "none"

    for rule in UMBRELLA_RULES:
        # chance_value is forced to 0 above when chance is missing, which
        # never meets a chance_threshold (20/40/60), so chance_fired can
        # only be true when a real chance value was reported.
        chance_fired = not chance_missing and chance_value >= rule["chance_threshold"]
        mm_fired = mm >= rule["mm_threshold"]
        if chance_fired or mm_fired:
            verdict = rule["verdict"]
            confidence = rule["confidence"]
            rule_fired = rule["rule_fired"]
            # Cite only the side(s) that actually crossed a threshold, so
            # the reason never claims a percentage or amount that had
            # nothing to do with the verdict.
            if chance_fired and mm_fired:
                reason = f"Precipitation chance is {chance_value}% and expected precipitation is {mm} mm."
            elif chance_fired:
                reason = f"Precipitation chance is {chance_value}%."
            else:
                reason = f"Expected precipitation is {mm} mm."
            break

    wind_warning = False

    if weather_code in SNOW_WEATHER_CODES:
        verdict = "no"
        rule_fired = "snow_override"
        reason = "Snow is expected, an umbrella is the wrong tool for snow, bring a hood or waterproof coat instead."

    if wind_kmh >= 40 and verdict in ("yes", "maybe"):
        wind_warning = True
        reason += f" Wind is forecast at {wind_kmh} km/h, a rain jacket beats an umbrella at that speed."

    if chance_missing and mm_missing:
        # Nothing to lean on at all - the default reason above already
        # says so - so confidence goes straight to the floor rather than
        # the single step a lone missing field costs below.
        confidence = "low"
    elif chance_missing:
        reason += " Precipitation chance was not reported, so this leans on the precipitation amount instead."
        confidence = _lower_confidence(confidence)

    return {
        "verdict": verdict,
        "confidence": confidence,
        "reason": reason,
        "rule_fired": rule_fired,
        "wind_warning": wind_warning,
        "inputs_used": {
            "precipitation_chance_pct": chance,
            "precipitation_mm": day.get("precipitation_mm"),
            "wind_max_kmh": day.get("wind_max_kmh"),
            "weather_code": weather_code,
        },
    }


def packing_list(day: dict) -> list[str]:
    """
    Build a short packing list for a single forecast day.

    Rules, applied in this order (an item is added at most once):
        - temp_low_c < 0 C -> "a heavy winter coat" (instead of a jacket)
        - else temp_low_c < 12 C -> "a jacket"
        - umbrella_advice verdict "yes" -> "an umbrella"
        - umbrella_advice snow override -> "waterproof boots"
        - uv_index_max >= 6 -> "sunscreen"
        - temp_high_c - temp_low_c > 12 C -> "layers, the day swings more than 12 C"
        - wind_max_kmh >= 40 -> "a windproof outer layer"

    Args:
        day: A forecast day dict as returned by weather_client.daily_forecast.

    Returns:
        A list of plain strings in the order above, empty if nothing applies.
    """
    items = []

    low = day.get("temp_low_c")
    if low is not None and low < 0:
        items.append("a heavy winter coat")
    elif low is not None and low < 12:
        items.append("a jacket")

    advice = umbrella_advice(day)
    if advice["verdict"] == "yes":
        items.append("an umbrella")
    if advice["rule_fired"] == "snow_override":
        items.append("waterproof boots")

    uv = day.get("uv_index_max")
    if uv is not None and uv >= 6:
        items.append("sunscreen")

    high = day.get("temp_high_c")
    if high is not None and low is not None and (high - low) > 12:
        items.append("layers, the day swings more than 12 C")

    wind = day.get("wind_max_kmh")
    if wind is not None and wind >= 40:
        items.append("a windproof outer layer")

    return items


def travel_score(day: dict, alerts: list[dict] | None = None) -> dict:
    """
    Score how good a day looks for travel, 0-100, starting at 100 and
    subtracting a penalty for each factor that fires.

    Penalties (only applied when the underlying field is not None):
        precipitation_chance_pct > 20  -> 0.4 * (chance - 20)
        precipitation_mm > 2           -> 2.0 * (mm - 2)
        wind_max_kmh > 30              -> 1.0 * (kmh - 30)
        temp_high_c > 35               -> 3.0 * (c - 35)
        temp_low_c < -5                -> 3.0 * (-5 - c)
        uv_index_max > 8               -> 5.0 flat
        alerts present                 -> 40 if any alert severity is
                                           "Severe" or "Extreme", else 15

    The score is clamped to 0..100. Bands: >= 80 "good", 60-79 "fair",
    40-59 "marginal", < 40 "poor" - except when none of the six fields
    above (precipitation_chance_pct, precipitation_mm, wind_max_kmh,
    temp_high_c, temp_low_c, uv_index_max) are present, in which case the
    day carries no signal to score at all: band is "unknown" and score is
    None, not a number. 0 is a real, meaningful score on this scale (an
    awful day), so it must not double as "no data"; a fully-empty day used
    to report a perfect 100 (every penalty above is guarded with "is not
    None", so absent data used to read as perfect weather), which was
    just as wrong in the other direction. Alerts are not scored in the
    "unknown" case either. See compare_days for how a None score ranks
    against real ones.

    Args:
        day: A forecast day dict as returned by weather_client.daily_forecast.
        alerts: Active alerts for the same location and day, as returned by
            weather_client.active_alerts()["alerts"]. None or [] if there
            are none, or if alerts could not be fetched.

    Returns:
        A dict with score (None when band is "unknown", otherwise 0-100),
        band, factors (each with points and why), headline, packing_list,
        alerts_considered, inputs_scored (how many of the six scoreable
        day fields were present), and inputs_available (six, the total
        number of scoreable dimensions).
    """
    inputs_available = len(SCOREABLE_FIELDS)
    inputs_scored = sum(1 for field in SCOREABLE_FIELDS if day.get(field) is not None)

    if inputs_scored == 0:
        return {
            "score": None,
            "band": "unknown",
            "factors": [],
            "headline": "No forecast data was available to score this day.",
            "packing_list": packing_list(day),
            "alerts_considered": 0,
            "inputs_scored": inputs_scored,
            "inputs_available": inputs_available,
        }

    score = 100.0
    factors = []

    chance = day.get("precipitation_chance_pct")
    if chance is not None and chance > 20:
        points = 0.4 * (chance - 20)
        factors.append({"points": round(points, 1), "why": f"Precipitation chance is {chance}%, above the 20% comfort threshold."})
        score -= points

    mm = day.get("precipitation_mm")
    if mm is not None and mm > 2:
        points = 2.0 * (mm - 2)
        factors.append({"points": round(points, 1), "why": f"Precipitation is {mm} mm, above the 2 mm comfort threshold."})
        score -= points

    wind = day.get("wind_max_kmh")
    if wind is not None and wind > 30:
        points = 1.0 * (wind - 30)
        factors.append({"points": round(points, 1), "why": f"Max wind is {wind} km/h, above the 30 km/h comfort threshold."})
        score -= points

    high = day.get("temp_high_c")
    if high is not None and high > 35:
        points = 3.0 * (high - 35)
        factors.append({"points": round(points, 1), "why": f"High temperature is {high} C, above the 35 C comfort threshold."})
        score -= points

    low = day.get("temp_low_c")
    if low is not None and low < -5:
        points = 3.0 * (-5 - low)
        factors.append({"points": round(points, 1), "why": f"Low temperature is {low} C, below the -5 C comfort threshold."})
        score -= points

    uv = day.get("uv_index_max")
    if uv is not None and uv > 8:
        points = 5.0
        factors.append({"points": points, "why": f"UV index is {uv}, above 8."})
        score -= points

    alerts = alerts or []
    alerts_considered = len(alerts)
    if alerts:
        severities = {a.get("severity") for a in alerts}
        points = 40.0 if severities & {"Severe", "Extreme"} else 15.0
        factors.append({"points": points, "why": f"{alerts_considered} active weather alert(s) for this location."})
        score -= points

    score = max(0, min(100, round(score)))

    if score >= 80:
        band = "good"
    elif score >= 60:
        band = "fair"
    elif score >= 40:
        band = "marginal"
    else:
        band = "poor"

    headline = f"Travel score {score}/100 ({band})."
    if factors:
        biggest = max(factors, key=lambda f: f["points"])
        headline += f" Biggest factor: {biggest['why']}"

    return {
        "score": score,
        "band": band,
        "factors": factors,
        "headline": headline,
        "packing_list": packing_list(day),
        "alerts_considered": alerts_considered,
        "inputs_scored": inputs_scored,
        "inputs_available": inputs_available,
    }


def compare_days(entries: list[dict]) -> dict:
    """
    Rank several locations/days against each other by travel_score.

    Args:
        entries: A list of dicts, each shaped {"label": str, "day": dict,
            "alerts": list[dict] | None, "alerts_status": str} (the last
            key is optional, defaulting to "ok" when absent).

    Returns:
        A dict with ranked (list of {"label", "score", "band", "headline",
        "alerts_status"}, sorted by score descending, ties broken by label
        ascending; an entry whose score is None - band "unknown", no
        scoreable data at all - always sorts after every entry with a real
        score, tied among themselves by label ascending too. There is
        nothing to say a no-data entry is better than one that actually
        scored, so it never ranks above one, even a "poor" one), best (top
        label), and worst (bottom label). best/worst are None for an empty
        entries list.
    """
    ranked = []
    for entry in entries:
        label = entry.get("label")
        day = entry.get("day") or {}
        alerts = entry.get("alerts")
        result = travel_score(day, alerts)
        ranked.append({
            "label": label,
            "score": result["score"],
            "band": result["band"],
            "headline": result["headline"],
            "alerts_status": entry.get("alerts_status", "ok"),
        })

    # A None score (no scoreable data) sorts after every real score:
    # (score is None) is False for a real score and True for None, and
    # False sorts before True. Within each group, higher score first, then
    # label ascending, same tie-break as before.
    ranked.sort(key=lambda r: (r["score"] is None, -(r["score"] or 0), r["label"] or ""))

    return {
        "ranked": ranked,
        "best": ranked[0]["label"] if ranked else None,
        "worst": ranked[-1]["label"] if ranked else None,
    }
