# Weather MCP server + Agent Bricks agent

Day 3 homework. Two Databricks Apps and one agent:

- `mcp_server/` is a [FastMCP](https://gofastmcp.com/) server that exposes eight weather tools
  over streamable HTTP, so a Databricks Agent Bricks agent can call them like any other tool.
- `dashboard/` is a small FastAPI app that shows what the agent has been asking for, and lets a
  human run the same lookups by hand to check the agent's answers.
- `agent/system_prompt.md` is the agent's instructions, including the guardrails that stop it
  inventing weather it never fetched.

Weather data comes from [Open-Meteo](https://open-meteo.com/). Severe weather alerts come from
the [US National Weather Service](https://www.weather.gov/documentation/services-web-api).
Neither needs an API key, so there is nothing to sign up for and no credential to leak.

## Architecture

```
Agent Bricks agent
      |
      | MCP tool calls (streamable HTTP)
      v
mcp_server/weather_mcp_server.py     <-- Databricks App #1
      |
      +-- weather_client.py  --HTTP-->  Open-Meteo (geocoding, forecast, archive)
      |                      --HTTP-->  api.weather.gov (alerts, US and territories)
      |
      +-- recommendations.py           pure threshold logic, no network
      |
      +-- query_log.py       --SQL-->   Lakebase table `weather_queries`
                                             ^
                                             |
dashboard/app.py  ---------------------------+   <-- Databricks App #2
```

The split follows the same shape as the Day 3 reference repo. One app answers tool calls, the
other serves a page for people. They deploy from separate folders, so each folder carries its
own copy of the shared modules and its own `requirements.txt`.

### Why the logic is split three ways

`weather_client.py` does all the HTTP work and nothing else. It calls Open-Meteo and the NWS,
parses the responses into flat dicts, and raises a typed exception when something goes wrong.
No `@mcp.tool` function ever touches `requests`.

`recommendations.py` holds the judgment calls: whether you need an umbrella, how good a day looks
for travel, what to pack. It takes parsed dicts and returns dicts. It imports nothing outside the
standard library, makes no network calls, and reads no environment variables, which is what makes
its thresholds straightforward to unit test.

`weather_mcp_server.py` is the thin layer between them. Each tool resolves a location, fetches
data, applies logic, and turns any failure into a `status` field the agent can read: `ok`,
`not_found`, `invalid_request`, `upstream_error`, or `error`. No tool body raises.

One case sits outside that promise. FastMCP validates arguments against the tool signature before
the function runs, so sending a string where a list belongs fails at the protocol level and never
produces a `status` field at all. The system prompt tells the agent what that looks like.

## The tools

| Tool | What it does |
| --- | --- |
| `resolve_location(query)` | Turns free text into coordinates. Returns the best match plus other candidates so the agent can ask which "Springfield" you meant. |
| `get_current_weather(location)` | Temperature, feels-like, humidity, wind, precipitation, conditions, right now. |
| `get_forecast(location, days)` | Daily highs, lows, rain chance, wind, UV, and each day's `weekday` name for the next 1 to 16 days. |
| `get_umbrella_advice(location, date)` | Derived judgment, not a passthrough. See the thresholds below. |
| `get_travel_recommendation(location, date)` | A 0 to 100 score with the penalty breakdown, a packing list, and any alerts that are still live on the day being scored, returned in full so the agent can name them. |
| `get_severe_weather_alerts(location)` | Active NWS alerts. United States and its territories only. Anywhere else comes back as `unsupported_region` rather than an error. |
| `compare_locations(locations, date)` | Ranks 2 to 5 places for the same day by travel score. |
| `get_historical_weather(location, start, end)` | Daily archive data plus averages and totals for the range. |

Every payload carries both metric and imperial values (`temperature_c` and `temperature_f`,
`wind_speed_kmh` and `wind_speed_mph`, and so on), so the agent never has to convert anything
itself.

### The umbrella thresholds

This is the tool that has to show reasoning rather than echo the API. It reads the day's rain
chance, expected rainfall, wind, and weather code, then applies these rules in order:

| Condition | Verdict | Confidence |
| --- | --- | --- |
| 60% chance or more, or 5 mm or more of rain | yes | high |
| 40% chance or more, or 1 mm or more | yes | medium |
| 20% chance or more, or 0.2 mm or more | maybe | medium |
| anything below that | no | high |

Then three overrides:

- Snow codes flip the verdict to "no". An umbrella is the wrong tool for snow, so the tool
  recommends a hood or a waterproof coat instead.
- Wind at 40 km/h or above keeps the verdict but sets `wind_warning`, because an umbrella will
  turn inside out and a rain jacket works better.
- A missing rain-chance figure drops the confidence one step and says why. If the forecast reports
  neither a chance nor an amount, confidence drops to low and the reason says only that, rather
  than claiming both figures were low.

The response names the rule that fired and lists the inputs it used, and the reason cites only the
figure that actually crossed a threshold. When 9 mm of rain triggers the verdict at a 3 percent
chance, it says so, instead of quoting a percentage that had nothing to do with the decision. The
agent repeats this sentence to users, so it has to survive being read aloud.

### The travel score

Starts at 100 and subtracts. Rain chance above 20% costs 0.4 points per percentage point. Rain
above 2 mm costs 2 points per mm. Wind above 30 km/h costs 1 point per km/h. Heat above 35 °C and
cold below -5 °C cost 3 points per degree. A UV index above 8 costs a flat 5. An active severe or
extreme alert costs 40, anything milder costs 15.

80 and above is good, 60 to 79 is fair, 40 to 59 is marginal, below 40 is poor. The response lists
every penalty that fired and what it cost, so the agent can explain the score.

Only alerts still live on the day being scored count. An advisory expiring this afternoon does not
dock points from a trip ten days out.

A day the forecast has no numbers for returns `score: null` and band `unknown`, not a score. Zero
is a real value on this scale and would read as "terrible day" rather than "nothing known", so the
tool declines to give one and says why in the headline. In a multi-city comparison, a location with
no data ranks below every location that scored, including a bad one.

## Running it locally

```bash
uv venv .venv --python 3.12
uv pip install --python .venv/bin/python -r mcp_server/requirements.txt
.venv/bin/python mcp_server/weather_mcp_server.py     # MCP on :8000
```

In a second terminal:

```bash
uv pip install --python .venv/bin/python -r dashboard/requirements.txt
.venv/bin/python dashboard/app.py                      # dashboard on :8001
```

Open `http://localhost:8001` and search for a city. Nothing needs configuring first: no API key,
no database, no Databricks login. The Lakebase query log is optional and stays switched off until
you point it at a database.

Run the tests with:

```bash
uv pip install --python .venv/bin/python -r requirements-dev.txt
.venv/bin/python -m pytest
```

The tests never touch the network. They feed canned API payloads through the parsing code and
check every threshold in `recommendations.py`.

## Timeouts and health checks

Both apps answer `GET /healthz` with `{"status": "ok"}`. On the MCP server that route sits
alongside `/mcp`, so a load balancer has something to poll that is not a JSON-RPC endpoint.

Upstream calls get 5 seconds to connect and 15 to respond, with one retry on a connection error,
a 5xx, or a 429. On top of that, each tool call carries a 60 second budget across every request it
makes. Without it, a comparison of five cities could sit through fifteen slow requests and spend
several minutes before answering, and the caller would see a dropped connection rather than the
error payload this design exists to produce. When the budget runs out mid-comparison, the
locations still waiting land in `failed` with a timeout message and the rest are still ranked.

## Configuration

Copy `.env.example` and edit it if you want to change anything. All of it is optional.

| Variable | Default | What it does |
| --- | --- | --- |
| `NWS_CONTACT_EMAIL` | a placeholder address | The NWS asks every caller to identify itself in the `User-Agent` header. Put a real address here before you deploy. |
| `NWS_CONTACT_SECRET_SCOPE` / `NWS_CONTACT_SECRET_KEY` | unset | Read the contact address from a Databricks secret instead of the environment, if you would rather not commit an email address. |
| `LAKEBASE_SECRET_SCOPE` / `LAKEBASE_SECRET_KEY` | unset | Where to find the Lakebase connection URL: the name of a Databricks secret scope and key holding it. Neither `app.yaml` sets them, since this repo ships with no Lakebase instance to point at. Leave them unset and query logging stays off. |
| `LAKEBASE_URL` | unset | A direct Postgres URL for local development, used instead of the secret. |
| `APP_NAME` | `weather-mcp-server` | The name sent in the `User-Agent` header. |
| `DATABRICKS_APP_PORT` / `PORT` | 8000 and 8001 | Set by Databricks Apps at runtime. |

There are no API keys in this project. If you swap Open-Meteo for a service that needs one,
store it with `databricks secrets put-secret` and read it through
`WorkspaceClient().secrets.get_secret()`, the way `_contact_email()` in `weather_client.py`
already does for the NWS address. Do not put it in `app.yaml`.

## The optional query log

`query_log.py` writes one row per tool call to a Lakebase table, which is what fills the recent
queries panel on the dashboard. It is genuinely optional. If no Lakebase URL resolves, the module
stays off and every tool keeps working. If it is configured but Lakebase cannot actually be
reached, a write or read failure disables it for the rest of the process and logs one warning; a
tool call still never fails because of it.

The dashboard reflects this honestly instead of guessing from an empty result: `/api/recent`
reports a `status` of `off` (not configured, nothing to do), `error` (configured, but the database
could not be reached), or `ok` (configured and working), and the recent-queries panel shows a
different message for each. An unreachable database renders as "the database could not be
reached", never as a silent "no queries yet".

To turn it on, point both apps at the same Lakebase instance by setting `LAKEBASE_URL`, or
`LAKEBASE_SECRET_SCOPE` / `LAKEBASE_SECRET_KEY` naming a Databricks secret that holds the
connection URL, then create the table:

```bash
psql "$LAKEBASE_URL" -f mcp_server/schema_weather_queries.sql
```

A logging failure can never fail a tool call: the write is wrapped, and after the first failure
the module stops trying.

## Deploying to Databricks

[`DEPLOY.md`](DEPLOY.md) has the full walkthrough: deploying both apps with the CLI, registering
the MCP server, and building the agent. The short version:

```bash
databricks sync mcp_server "/Workspace/Users/$U/weather-mcp" -p $P
databricks apps create weather-mcp -p $P
databricks apps deploy weather-mcp --source-code-path "/Workspace/Users/$U/weather-mcp" -p $P
```

Repeat for `dashboard/`. Syncing one folder at a time is what puts each app's `app.yaml` at the
root of its own source path.

An Agent Bricks agent does not take a URL. It takes a Unity Catalog HTTP connection created with
`is_mcp_connection 'true'`, which authenticates to the app over OAuth as a service principal, and
that connection is then attached to the agent as a `uc_connection` tool. `DEPLOY.md` walks through
the service principal, the secret, the `CREATE CONNECTION` statement, and a `http_request` call
that proves the connection can list all eight tools before you involve the agent at all.

Set `NWS_CONTACT_EMAIL` in both `app.yaml` files to a real address first. The National Weather
Service blocks callers that do not identify themselves, and this is the one misconfiguration that
degrades quietly instead of failing.

## Files

- `mcp_server/weather_mcp_server.py` - FastMCP entrypoint and the eight tool definitions
- `mcp_server/weather_client.py` - all HTTP calls and response parsing
- `mcp_server/recommendations.py` - umbrella, travel score, packing list, comparison logic
- `mcp_server/query_log.py` - best-effort Lakebase logging, disables itself on failure
- `mcp_server/lakebase.py` - Postgres connection helper
- `mcp_server/schema_weather_queries.sql` - the one table the dashboard reads
- `mcp_server/app.yaml`, `mcp_server/requirements.txt` - Databricks App config
- `dashboard/app.py` - FastAPI dashboard, read only
- `dashboard/templates/index.html` - the page, with the CSS and JS inlined
- `dashboard/` also carries copies of `weather_client.py`, `recommendations.py`, `query_log.py`,
  and `lakebase.py`, because each Databricks App deploys from its own folder and there is no
  shared install step between them
- `agent/system_prompt.md` - the agent instructions
- `agent/transcripts.md` - example questions and what the agent did with them
- `tests/` - pytest suite, no network
