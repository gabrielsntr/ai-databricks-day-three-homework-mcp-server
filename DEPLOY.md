# Deploying to Databricks

Everything here runs from the repo root with the Databricks CLI. The commands were checked
against CLI v1.11.0 and an AWS workspace. Azure and GCP differ in two places, both called out
below.

Set these once per shell so you can paste the rest without editing:

```bash
export P=gabriel-macos                          # your ~/.databrickscfg profile
export U=gabriellsantoro@gmail.com              # your workspace user
export WS=https://dbc-a842b221-2cfd.cloud.databricks.com
```

Check the profile works before going further:

```bash
databricks current-user me -p $P
```

## 1. Set a real contact address

The National Weather Service blocks callers that do not identify themselves. Both apps send a
`User-Agent` built from `APP_NAME` and `NWS_CONTACT_EMAIL`, and both `app.yaml` files ship with
`change-me@example.com`.

Edit `mcp_server/app.yaml` and `dashboard/app.yaml` and put a real address in each. Do this
first. It is the one step that will silently degrade rather than fail loudly.

## 2. Deploy the two apps

Each app deploys from its own workspace directory. Sync only the one folder, so `app.yaml` lands
at the root of the app's source path where the runtime looks for it.

```bash
# MCP server
databricks sync mcp_server "/Workspace/Users/$U/weather-mcp" -p $P
databricks apps create weather-mcp -p $P
databricks apps deploy weather-mcp --source-code-path "/Workspace/Users/$U/weather-mcp" -p $P
```

```bash
# Dashboard
databricks sync dashboard "/Workspace/Users/$U/weather-dashboard" -p $P
databricks apps create weather-dashboard -p $P
databricks apps deploy weather-dashboard --source-code-path "/Workspace/Users/$U/weather-dashboard" -p $P
```

`databricks apps deploy` uploads the code, applies the config, and starts the app. Get the URLs
with:

```bash
databricks apps list -p $P
```

Check both are alive:

```bash
curl -s https://weather-mcp-<id>.aws.databricksapps.com/healthz
curl -s https://weather-dashboard-<id>.aws.databricksapps.com/healthz
```

Those URLs sit behind workspace single sign-on, so a browser will work after you log in and a
bare `curl` will get a login redirect. That is expected.

Re-deploy after any code change by repeating the `sync` and `deploy` pair.

### If an app does not come up

`databricks apps logs weather-mcp -p $P` is the first place to look. Only stdout and stderr are
captured, so anything written to a log file is lost.

A 502 almost always means the process bound to the wrong port or interface. Both apps read
`DATABRICKS_APP_PORT` and bind `0.0.0.0`, so this should not happen, but it is the first thing
to rule out.

Apps have ten minutes to install dependencies and start.

## 3. Register the MCP server as a tool

This is the part that is not obvious. An Agent Bricks agent does not take a URL. It takes a Unity
Catalog HTTP connection, and the connection authenticates to your app with OAuth as a service
principal. So there are three things to create, in order.

### 3a. A service principal that may call the app

Create one, or reuse an existing one, and generate an OAuth secret for it. In the workspace UI:
**Settings** > **Identity and access** > **Service principals**. Note the **client ID** and the
**client secret**, which is shown once.

Then grant it access to the MCP app:

```bash
databricks apps set-permissions weather-mcp -p $P --json '{
  "access_control_list": [
    {"service_principal_name": "<client-id>", "permission_level": "CAN_USE"}
  ]
}'
```

### 3b. Store the secret

```bash
databricks secrets create-scope weather-mcp -p $P
databricks secrets put-secret weather-mcp oauth-client-secret --string-value '<client-secret>' -p $P
```

### 3c. Create the connection

Run this in a SQL editor or a notebook. Reference the secret with `secret()` rather than pasting
the value, so it stays out of query history and out of `SHOW CREATE CONNECTION`.

```sql
CREATE CONNECTION weather_mcp TYPE HTTP
OPTIONS (
  host 'https://weather-mcp-<id>.aws.databricksapps.com',
  port '443',
  base_path '/mcp',
  client_id '<client-id>',
  client_secret secret('weather-mcp', 'oauth-client-secret'),
  oauth_scope 'all-apis',
  token_endpoint 'https://dbc-a842b221-2cfd.cloud.databricks.com/oidc/v1/token',
  is_mcp_connection 'true'
);
```

`base_path` is `/mcp` with no trailing slash. A trailing slash answers with a 307 redirect, and
not every client follows a redirect on a POST.

On Azure the `token_endpoint` host ends in `azuredatabricks.net` and the app domain is
`azure.databricksapps.com`.

Confirm the connection can reach the server and that all eight tools come back:

```sql
SELECT http_request(
  conn => 'weather_mcp',
  method => 'POST',
  path => '',
  json => '{"jsonrpc":"2.0","method":"tools/list","id":1}'
);
```

Then grant the agent's service principal access:

```sql
GRANT USE CONNECTION ON CONNECTION weather_mcp TO `<agent-service-principal>`;
```

## 4. Build the agent

A supervisor agent routes to tools, and a UC connection is one of the tool types it accepts.

```bash
databricks supervisor-agents create-supervisor-agent "Weather assistant" \
  --description "Answers weather questions and gives travel and umbrella advice" \
  --instructions "$(sed -n '/^---$/,$p' agent/system_prompt.md | tail -n +2)" \
  -p $P
```

That `sed` pulls everything below the `---` marker in `agent/system_prompt.md`, which is the part
meant for the agent. The lines above it are notes for people.

The command returns a name like `supervisor-agents/<uuid>`. Attach the MCP server:

```bash
databricks supervisor-agents create-tool supervisor-agents/<uuid> weather --json '{
  "tool_type": "uc_connection",
  "description": "Current conditions, multi-day forecasts, umbrella advice, travel scoring, severe weather alerts for US locations, multi-city comparison, and historical weather. Use for any question about weather anywhere in the world.",
  "uc_connection": {"name": "weather_mcp"}
}' -p $P
```

The description drives routing, so keep it specific about what the tools cover.

Add the demo questions as examples:

```bash
databricks supervisor-agents create-example supervisor-agents/<uuid> --json '{
  "question": "Will it rain in Chicago tomorrow?",
  "guidelines": ["Call get_forecast first to learn the local dates, then get_umbrella_advice for that date", "Quote the figure the reason cites, not the other one"]
}' -p $P
```

The serving endpoint takes up to ten minutes to come online after creation. Watch it with:

```bash
databricks supervisor-agents get-supervisor-agent supervisor-agents/<uuid> -p $P
databricks serving-endpoints get <endpoint-name> -p $P
```

Once it answers, work through the six questions in `agent/transcripts.md` and paste the replies
into the empty blocks.

## 5. The query log, if you want it

Skip this unless you want the dashboard's recent-queries panel to fill in. Everything else works
without it.

Create a Lakebase instance and a Postgres role with a static password, then store the connection
URL:

```bash
databricks secrets create-scope database -p $P
databricks secrets put-secret database lakebase-url \
  --string-value 'postgresql://<role>:<password>@<host>.database.cloud.databricks.com:5432/databricks_postgres?sslmode=require' -p $P
```

Create the table:

```bash
psql "<the same URL>" -f mcp_server/schema_weather_queries.sql
```

Both apps already point at `database` and `lakebase-url` in their `app.yaml`. Their service
principals need READ on that scope, and you need MANAGE on it to grant that. Restart both apps
afterwards.

If any of this is missing or wrong, `query_log` logs one warning and turns itself off. No tool
call fails because of it.

## Things that will bite you

**Outbound internet.** Both apps call `open-meteo.com` and `api.weather.gov`. If your workspace
has an egress policy, those two hosts need allowlisting, and an egress change needs an app
restart to take effect. Everything will look healthy and every weather call will fail.

**The 120 second proxy limit.** The Apps reverse proxy caps every request at 120 seconds and is
not configurable. The 504 is generated at the proxy, so nothing appears in the app logs. Each
tool call here carries a 60 second budget across all its upstream requests, which keeps it under
the cap, but it is worth knowing why that budget exists.

**Secret scope permissions.** An app's service principal needs READ on any scope it reads, and
you need MANAGE on the scope to grant it. This is the most common cause of an app that deploys
cleanly and then throws `PERMISSION_DENIED` at runtime.

**Compute.** Apps consume DBUs while running. Stop them when you are not demoing:

```bash
databricks apps stop weather-mcp -p $P
databricks apps stop weather-dashboard -p $P
```

## Not verified here

Two things in this repo were not tested against a live workspace, because that needs your
credentials.

`query_log` attributes rows to a user by reading the `x-forwarded-user` and `x-forwarded-email`
request headers. The documented header for app requests is `x-forwarded-access-token`, and the
two identity headers come from the reference project rather than from current documentation. If
they are not injected, `requested_by` is null and nothing else changes.

The exact `create-tool` behaviour for a `uc_connection` pointing at an MCP server is documented
but was not run end to end here. If the agent cannot see the tools, the `http_request` check in
step 3c is the place to start, because it isolates the connection from the agent.
