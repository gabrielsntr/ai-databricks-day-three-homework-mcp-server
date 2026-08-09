# Build Your Own Weather MCP Server

## Homework: Build Your Own Weather-Prediction MCP Server + Agent

**Date:** 2026-08-08
**Based on:** Day 3 (databricks-lakebase-app-day-3) - Agent Bricks + Alpaca Markets paper-trading MCP server

### TL;DR

Using this repo as a reference pattern (not a template to copy verbatim), build your own MCP server that exposes weather-forecast tools, and wire a Databricks Agent Bricks agent to use it to answer weather questions and make simple predictions/recommendations. You'll deploy both as Databricks Apps, same as Day 3's `mcp_server/` + `dashboard/` split.

### What you're building

- An MCP server (FastMCP, same as `mcp_server/alpaca_mcp_server.py`) exposing weather tools backed by a free weather API (no paid tier, no credit card required to start).
- A broker/adapter module (same role as `alpaca_broker.py`) that calls the weather API and returns clean dicts — keep your MCP tool functions thin, push the HTTP/parsing logic into this module.
- A Databricks Agent Bricks agent that uses your MCP server as an external tool to answer natural-language weather questions (e.g. "Will it rain in Chicago tomorrow?", "Should I bring a jacket to Austin this weekend?").
- (Optional stretch) A small dashboard app (like `dashboard/`) that shows recent agent queries/predictions — not required for a passing grade, but nice for extra credit.

### Suggested free weather APIs (pick one)

- **Open-Meteo** — no signup, no API key, ~10,000 calls/day (non-commercial)
- **National Weather Service API (weather.gov)** — no signup, no API key, US-only. Official NOAA data — great for alerts + forecasts, but only works for US locations.
- **WeatherAPI.com** — API key (free signup), 100,000 calls/month. Good if you want current + forecast + historical in one call, and don't mind a quick signup.

**Recommendation:** start with Open-Meteo — it needs zero credentials, so you can build and test the whole pipeline before worrying about secrets management at all. If you want alerts or US-specific severe weather data, layer in the NWS API as a second tool.

### Required MCP tools (minimum 3)

Design your own tool names/signatures, but your MCP server must expose at least these three capabilities (model them after `get_quote`/`get_positions`/`get_account_summary` in `mcp_server/alpaca_mcp_server.py`):

1. **Current conditions** — e.g. `get_current_weather(location)` — temperature, conditions, humidity, wind for a given location (city name, zip, or lat/lon — your choice).
2. **Forecast** — e.g. `get_forecast(location, days)` — a multi-day forecast (temp high/low, precipitation chance, conditions) for the next N days.
3. **Simple prediction/recommendation** — e.g. `predict_umbrella_needed(location, date)` or `get_travel_recommendation(location, date)` — some derived judgment call built from the raw forecast data (e.g. "bring an umbrella if precipitation chance > 40%"). This is where you show reasoning, not just a passthrough of the raw API response.

**Stretch tools (optional, for extra credit):** severe weather alerts, historical weather lookup, comparing weather across multiple cities.

### Requirements checklist

- MCP server built with FastMCP (or another MCP-compliant framework), exposing your tools via `@mcp.tool` decorators, following the streamable-HTTP pattern from `mcp_server/alpaca_mcp_server.py`.
- A separate adapter module (like `alpaca_broker.py`) containing all HTTP calls/parsing — no raw `requests` calls inside your `@mcp.tool` functions.
- If your chosen API requires a key: store it as a Databricks secret, never hardcode it or commit it to the repo. Follow the `_secret()` / `WorkspaceClient().secrets.get_secret()` pattern in `mcp_server/alpaca_broker.py`.
- `requirements.txt` and `app.yaml` for your MCP server app (see `mcp_server/` for the pattern), deployed as its own Databricks App.
- A Databricks Agent Bricks agent registered against your MCP server as an external tool (same steps as Day 3's README, section "Register the MCP server as an external MCP" and "Build the Agent Bricks agent").
- A clear system prompt for your agent describing what it should do, which tools to call in what order, and any guardrails (e.g. "only answer for locations you can resolve; if the API call fails, say so rather than guessing").
- A short `README.md` for your submission (architecture diagram optional but encouraged, list of tools, setup steps, and which weather API + auth method you used).
- Demonstrate the agent working: paste or screenshot at least 3 different natural-language questions and the agent's tool-calling + final answers.

### What "good" looks like

- Tool functions have clear docstrings (Args/Returns), matching the style in `mcp_server/alpaca_mcp_server.py`.
- Error handling: a bad location or API outage returns a clean error, not a stack trace, and the agent can react sensibly (e.g. ask the user to clarify).
- The "prediction" tool does more than echo the raw API — it applies some threshold/logic of your choosing and explains it in the tool's docstring.
- No secrets committed to git. No hardcoded API keys.
- The agent's system prompt is specific enough that the agent doesn't hallucinate weather data it didn't get from a tool call.

### Submission

Push your MCP server + agent config (system prompt, tool list) to your own repo/branch and share the link, along with your Databricks App URLs (or screenshots if you can't share workspace access). Include your README.