# Databricks notebook source
# MAGIC %md
# MAGIC # Testing the weather MCP server and agent
# MAGIC
# MAGIC Run this top to bottom inside the workspace. It checks the three layers separately, so when
# MAGIC something breaks you know which one broke:
# MAGIC
# MAGIC 1. The Unity Catalog connection can reach the app and list its tools.
# MAGIC 2. Individual tools return sensible weather data.
# MAGIC 3. The agent picks the right tools and answers in plain language.
# MAGIC
# MAGIC Test 1 is the one to run first. If the connection is broken, everything above it fails in a
# MAGIC way that looks like an agent problem but is not.

# COMMAND ----------

CONNECTION = "weather_mcp"
ENDPOINT = "mas-59b8c794-endpoint"
APP_URL = "https://weather-mcp-7474659021054366.aws.databricksapps.com"

# COMMAND ----------

# MAGIC %md
# MAGIC ## Test 1: can the connection reach the server
# MAGIC
# MAGIC `http_request` sends a raw MCP call through the Unity Catalog connection. A 200 with a list of
# MAGIC tools means the OAuth handshake, the app, and the MCP server are all working.
# MAGIC
# MAGIC The `Accept` header is required. Streamable HTTP refuses a request that does not accept both
# MAGIC `application/json` and `text/event-stream`, and answers 406 without it.

# COMMAND ----------

# MAGIC %sql
# MAGIC SELECT http_request(
# MAGIC   conn => 'weather_mcp',
# MAGIC   method => 'POST',
# MAGIC   path => '',
# MAGIC   json => '{"jsonrpc":"2.0","method":"tools/list","id":1}',
# MAGIC   headers => map('Accept', 'application/json, text/event-stream')
# MAGIC ) AS response

# COMMAND ----------

# MAGIC %md
# MAGIC That response is dense. This pulls the tool names out of it.

# COMMAND ----------

import json
import re


def mcp_call(method: str, params: dict | None = None) -> dict:
    """Send one MCP request through the Unity Catalog connection and return the parsed result."""
    body = {"jsonrpc": "2.0", "method": method, "id": 1}
    if params is not None:
        body["params"] = params
    escaped = json.dumps(body).replace("'", "''")
    row = spark.sql(
        f"""
        SELECT http_request(
          conn => '{CONNECTION}',
          method => 'POST',
          path => '',
          json => '{escaped}',
          headers => map('Accept', 'application/json, text/event-stream')
        ) AS response
        """
    ).collect()[0]["response"]

    outer = json.loads(row)
    if outer.get("status_code") != "200":
        raise RuntimeError(f"HTTP {outer.get('status_code')}: {outer.get('text')[:400]}")

    # Streamable HTTP replies as server-sent events, so the JSON sits on a "data:" line.
    match = re.search(r"data: (\{.*)", outer["text"])
    return json.loads(match.group(1))["result"]


tools = mcp_call("tools/list")["tools"]
print(f"{len(tools)} tools registered\n")
for t in tools:
    print(f"  {t['name']:28} {t['description'].splitlines()[0]}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Test 2: do the tools return real weather
# MAGIC
# MAGIC These are the three capabilities the homework requires: current conditions, a forecast, and a
# MAGIC derived prediction. The third is the interesting one, because it applies thresholds rather than
# MAGIC passing the API response through.

# COMMAND ----------

def call_tool(name: str, **arguments):
    """Call one MCP tool and return its payload as a dict."""
    result = mcp_call("tools/call", {"name": name, "arguments": arguments})
    payload = result["structuredContent"].get("result", result["structuredContent"])
    return json.loads(payload) if isinstance(payload, str) else payload


current = call_tool("get_current_weather", location="Chicago")
print("Current conditions in Chicago")
print(f"  {current['temperature_c']} C / {current['temperature_f']} F, {current['conditions']}")
print(f"  feels like {current['feels_like_c']} C, humidity {current['humidity_pct']}%")
print(f"  wind {current['wind_speed_kmh']} km/h {current['wind_direction_compass']}")

# COMMAND ----------

forecast = call_tool("get_forecast", location="Chicago", days=5)
print(f"Forecast for {forecast['location']['label']} ({forecast['timezone']})\n")
for day in forecast["days"]:
    print(
        f"  {day['date']}  {day['temp_low_c']:>5} to {day['temp_high_c']:>5} C  "
        f"rain {str(day['precipitation_chance_pct']):>4}%  {day['conditions']}"
    )

# COMMAND ----------

# MAGIC %md
# MAGIC The prediction tool. `reason` names only the figure that crossed a threshold, and `rule_fired`
# MAGIC says which rule did it, so the answer can be audited rather than taken on trust.

# COMMAND ----------

tomorrow = forecast["days"][1]["date"]
advice = call_tool("get_umbrella_advice", location="Chicago", date=tomorrow)["advice"]

print(f"Umbrella advice for Chicago on {tomorrow}")
print(f"  verdict:    {advice['verdict']}  (confidence {advice['confidence']})")
print(f"  reason:     {advice['reason']}")
print(f"  rule fired: {advice['rule_fired']}")
print(f"  inputs:     {advice['inputs_used']}")

# COMMAND ----------

# MAGIC %md
# MAGIC A travel score, which combines several factors and shows what each one cost.

# COMMAND ----------

rec = call_tool("get_travel_recommendation", location="Denver", date=tomorrow)
r = rec["recommendation"]
print(f"Denver on {tomorrow}: {r['score']}/100 ({r['band']})")
for f in r["factors"]:
    print(f"  -{f['points']:>5} points   {f['why']}")
print(f"  pack: {', '.join(r['packing_list']) or 'nothing special'}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Test 3: does the error handling hold
# MAGIC
# MAGIC A bad location has to come back as a clean status the agent can act on, not a stack trace.
# MAGIC Alerts outside the United States have to say so rather than reporting a failure.

# COMMAND ----------

bad = call_tool("get_current_weather", location="Xyzzyville Nonexistentstan")
print("Unknown location:", bad["status"], "|", bad["message"])
print("hint:", bad.get("hint"))

paris = call_tool("get_severe_weather_alerts", location="Paris, France")
print("\nParis alerts:", paris["status"], "| coverage:", paris["coverage"])

miami = call_tool("get_severe_weather_alerts", location="Miami, FL")
print("Miami alerts:", miami["status"], "| coverage:", miami["coverage"], "| count:", miami["count"])
for a in miami["alerts"]:
    print(f"  {a['severity']}: {a['event']} for {a['area']}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Test 4: the agent
# MAGIC
# MAGIC Databricks makes the agent ask permission before it runs an MCP tool. It returns an
# MAGIC `mcp_approval_request`, and the caller approves before the tool runs. In the Agent Bricks
# MAGIC playground that is a button. Here the helper approves automatically and loops until the agent
# MAGIC produces an answer.

# COMMAND ----------

from databricks.sdk import WorkspaceClient

w = WorkspaceClient()


def ask_agent(question: str, max_rounds: int = 6) -> dict:
    """Ask the agent a question, approving each tool call, and return the answer plus the calls made."""
    conversation = [{"role": "user", "content": question}]
    calls = []

    for _ in range(max_rounds):
        response = w.api_client.do(
            "POST", f"/serving-endpoints/{ENDPOINT}/invocations", body={"input": conversation}
        )
        output = response.get("output", [])
        approvals = [o for o in output if o.get("type") == "mcp_approval_request"]

        if not approvals:
            for item in output:
                if item.get("type") == "message":
                    for chunk in item.get("content", []):
                        if chunk.get("type") == "output_text":
                            return {"answer": chunk["text"], "calls": calls}
            return {"answer": "(the agent returned no text)", "calls": calls}

        conversation += output
        for approval in approvals:
            calls.append((approval["name"], approval["arguments"]))
            conversation.append(
                {
                    "type": "mcp_approval_response",
                    "approval_request_id": approval["id"],
                    "approve": True,
                }
            )

    return {"answer": "(gave up after too many tool rounds)", "calls": calls}


def show(question: str) -> None:
    result = ask_agent(question)
    print("=" * 78)
    print("Q:", question)
    for name, args in result["calls"]:
        print(f"   tool: {name}({args})")
    print("A:", result["answer"])
    print()

# COMMAND ----------

show("Will it rain in Chicago tomorrow?")

# COMMAND ----------

show("Should I bring a jacket to Austin this weekend?")

# COMMAND ----------

show("Is there any severe weather in Miami right now?")

# COMMAND ----------

show("Where has better weather next Tuesday, Denver or Seattle?")

# COMMAND ----------

# MAGIC %md
# MAGIC The disambiguation guardrail. Springfield is ambiguous, so the agent should ask which one you
# MAGIC mean instead of picking a city for you.

# COMMAND ----------

show("What is the weather in Springfield?")

# COMMAND ----------

# MAGIC %md
# MAGIC ## If something fails
# MAGIC
# MAGIC | Symptom | Where to look |
# MAGIC | --- | --- |
# MAGIC | Test 1 returns 401 or 403 | The connection's service principal has lost `CAN_USE` on the app. |
# MAGIC | Test 1 returns 406 | The `Accept` header is missing. Both media types are required. |
# MAGIC | Test 1 says "Missing session ID" | The server is running in stateful mode. It should be built with `stateless_http=True`. |
# MAGIC | Test 1 returns 404 | The connection's `base_path` is wrong. It should be `/mcp` with no trailing slash. |
# MAGIC | Tools return `upstream_error` | Open-Meteo or weather.gov is unreachable from the app. Check any egress policy. |
# MAGIC | Alerts return `unsupported_region` for a US place | The location resolved to the wrong country. Check what `resolve_location` returns. |
# MAGIC | The agent answers without calling a tool | Its instructions were not saved, or the tool is not attached. |
# MAGIC | The agent answers about today when asked about tomorrow | It called a tool without the `date` argument, which defaults to today. |
