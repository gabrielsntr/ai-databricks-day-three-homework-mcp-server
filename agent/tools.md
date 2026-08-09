# Agent tool list

The tools the "Weather assistant" agent has enabled, exactly as the deployed MCP server reports
them. Signatures below were read from a live `tools/list` call against the running app, not copied
from the source, so they are what the agent actually sees.

The agent's instructions live in [`system_prompt.md`](system_prompt.md). Captured questions and
answers are in [`transcripts.md`](transcripts.md).

## How the tools reach the agent

The agent does not hold a URL. It holds a Unity Catalog connection, and the connection
authenticates to the Databricks App over OAuth as a service principal.

```
Weather assistant  (supervisor agent, endpoint mas-59b8c794-endpoint)
    |
    | tool_type: uc_connection
    v
weather_mcp  (Unity Catalog HTTP connection, is_mcp_connection = true)
    |
    | OAuth client credentials, service principal with CAN_USE on the app
    v
weather-mcp  (Databricks App, FastMCP over streamable HTTP at /mcp)
```

All eight tools are enabled. There is no curated subset: none of them writes anything or costs
money, so there is nothing to withhold.

| Setting | Value |
| --- | --- |
| Agent | `Weather assistant` |
| Serving endpoint | `mas-59b8c794-endpoint` |
| Tool id on the agent | `weather` |
| Tool type | `uc_connection` |
| Connection | `weather_mcp` |
| App endpoint | `https://weather-mcp-7474659021054366.aws.databricksapps.com/mcp` |

## The tools

### resolve_location(query: string)

Turns free text into coordinates. Accepts a place name such as "Chicago" or "Austin, TX", or a
bare `lat,lon` pair. Returns the best match plus up to four `alternatives`, which is what lets the
agent ask which "Springfield" you meant instead of picking one.

### get_current_weather(location: string)

Temperature, feels-like, humidity, wind speed and direction, pressure, precipitation, and a
conditions description for right now. Carries `observed_at` and `weekday` in the location's own
time zone.

### get_forecast(location: string, days: integer = 5)

Daily forecast for the next 1 to 16 days. Each day carries its date and weekday, high and low,
feels-like high, precipitation amount and chance, maximum wind and gust, UV index, sunrise, and
sunset.

The `weekday` field exists because the agent used to compute weekdays itself and got them wrong.
Asked for "next Tuesday" it picked a Wednesday. Giving it the name directly removed the whole
class of error.

### get_umbrella_advice(location: string, date: string = today)

The required prediction tool. Applies fixed thresholds rather than passing the forecast through:

| Condition | Verdict | Confidence |
| --- | --- | --- |
| 60% chance or more, or 5 mm or more | yes | high |
| 40% chance or more, or 1 mm or more | yes | medium |
| 20% chance or more, or 0.2 mm or more | maybe | medium |
| below that | no | high |

Three overrides. Snow flips the verdict to no, because an umbrella is the wrong tool for snow.
Wind at 40 km/h or above sets `wind_warning`, because an umbrella will invert. A missing rain
chance lowers confidence and says so.

Returns `rule_fired` and `inputs_used` alongside the verdict, and a `reason` that cites only the
figure that actually crossed a threshold. The agent quotes that sentence, so it has to survive
being read aloud.

### get_travel_recommendation(location: string, date: string = today)

A 0 to 100 score, starting at 100 and subtracting for rain chance above 20%, rain above 2 mm,
wind above 30 km/h, heat above 35 C, cold below -5 C, a UV index above 8, and any active alert.
80 and up is good, 60 to 79 fair, 40 to 59 marginal, below 40 poor.

Returns every penalty with what it cost, a packing list, and the alerts still live on the day
being scored. A day with no forecast data returns a null score and the band `unknown` rather than
a misleading number.

### get_severe_weather_alerts(location: string)

Active National Weather Service alerts, which cover the United States and its territories only.
Anywhere else returns `coverage: unsupported_region` rather than an error, so the agent can say it
has no feed for that country instead of claiming the service is down.

### compare_locations(locations: array of string, date: string = today)

Ranks 2 to 5 places for the same day by travel score. Locations that fail to resolve are reported
in `failed` and the rest are still ranked. Fewer than two survivors is an error, because a
comparison of one is not a comparison.

### get_historical_weather(location: string, start_date: string, end_date: string)

Daily archive observations plus averages and totals. The archive lags about five days behind
today, and a range outside what it holds comes back as `invalid_request` carrying the valid range
from the upstream service.

## Result shape

Every tool returns a `status` field: `ok`, `not_found`, `invalid_request`, `upstream_error`, or
`error`. No tool body raises. The one exception is an argument of the wrong type, which the MCP
layer rejects before the tool runs, so no `status` is produced; the system prompt tells the agent
what that looks like.

Every payload carries both metric and imperial values, so the agent never converts anything
itself.
