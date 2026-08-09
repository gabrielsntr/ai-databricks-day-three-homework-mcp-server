# Deploying to Databricks

Everything here runs from the repo root with the Databricks CLI. The commands were checked
against CLI v1.11.0 and an AWS workspace. Azure and GCP differ in two places, both called out
below.

Set these once per shell so you can paste the rest without editing:

```bash
export P=gabriel-macos                          # your ~/.databrickscfg profile
export U="$(databricks current-user me -p $P | python3 -c 'import sys,json; print(json.load(sys.stdin)["userName"])')"
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

Two ways to do this. The Git path is better for this repo, because Apps can deploy a single
subdirectory straight from GitHub and you never have to keep a workspace copy in step.

### Option A: straight from GitHub (recommended)

`source_code_path` inside `git_source` points at a subdirectory, and the app treats that
directory as its root and cannot see anything outside it. That is exactly the two-apps-one-repo
case.

```bash
# MCP server
databricks apps create weather-mcp -p $P --json '{
  "git_repository": {"url": "https://github.com/gabrielsntr/ai-databricks-day-three-homework-mcp-server", "provider": "gitHub"}
}'
databricks apps deploy weather-mcp -p $P --json '{
  "git_source": {"branch": "main", "source_code_path": "mcp_server"}
}'
```

```bash
# Dashboard
databricks apps create weather-dashboard -p $P --json '{
  "git_repository": {"url": "https://github.com/gabrielsntr/ai-databricks-day-three-homework-mcp-server", "provider": "gitHub"}
}'
databricks apps deploy weather-dashboard -p $P --json '{
  "git_source": {"branch": "main", "source_code_path": "dashboard"}
}'
```

Redeploy after a push by repeating the `deploy` command.

### Option B: sync from your laptop

Useful for trying a change you have not committed. Sync one folder at a time, so `app.yaml` lands
at the root of the app's source path.

```bash
databricks sync mcp_server "/Workspace/Users/$U/weather-mcp" -p $P
databricks apps deploy weather-mcp --source-code-path "/Workspace/Users/$U/weather-mcp" -p $P
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

There is a UI path and a SQL path. They build the same thing: a Unity Catalog HTTP connection,
plus an MCP service that is itself a UC securable addressed as `catalog.schema.name`.

For the UI: **AI Gateway** > **MCPs** > **Register MCP Server**, choose the external option, and
give it `https://weather-mcp-<id>.aws.databricksapps.com/mcp` as the endpoint. Pick the shared
principal auth mode and give it the service principal from 3a. Then grant the agent access with
`GRANT EXECUTE ON MCP SERVICE <catalog>.<schema>.<name> TO ...`.

For SQL, run this in a SQL editor or a notebook. Reference the secret with `secret()` rather than
pasting the value, so it stays out of query history and out of `SHOW CREATE CONNECTION`.

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

**Python is 3.11, and you cannot pin it.** Apps that install from `requirements.txt` get 3.11.
Only a `pyproject.toml` and `uv.lock` setup can request a different version. The whole test suite
passes on 3.11, so this repo is fine, but keep it in mind before reaching for newer syntax.

**Secrets arrive in two different encodings.** `WorkspaceClient().secrets.get_secret()` returns a
base64-encoded value that the caller has to decode, which is what `_contact_email()` and
`lakebase.py` do. The other route, declaring a secret resource in `databricks.yml` and pulling it
into `app.yaml` with `valueFrom`, injects the plaintext directly. If you ever switch a secret to
the `valueFrom` route, delete the base64 decode for it or you will corrupt the value.

**Outbound internet.** Both apps call `open-meteo.com` and `api.weather.gov`. This works by
default. Egress restriction is an opt-in Enterprise-tier network policy, so most workspaces have
nothing to configure. If yours does restrict egress, allowlist those two hosts and restart the
apps, because an egress change needs a restart. Everything will look healthy and every weather
call will fail until you do.

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

Four things could not be checked without your workspace. Treat them as the places to look first
if something misbehaves.

**User attribution records the caller, which for agent traffic is a service principal.** This was
listed here as probably broken, on the grounds that the current Apps documentation mentions only
`x-forwarded-access-token`. Measured against the running deployment, the headers `query_log`
reads do arrive and `requested_by` is populated. What lands in it is the identity that called the
app, and when the caller is the Agent Bricks agent that is the connection's service principal
client ID, not the person who typed the question. Rows written by an agent therefore all carry
the same value.

Attributing a row to the human behind the agent would mean the agent forwarding the end user's
identity to the tool call, which the MCP tool surface does not carry today. Direct browser
traffic to the dashboard is a different matter and does identify the person.

**Registering a Databricks App as an "external" MCP server is thinly documented.** The external
MCP docs are written for third-party SaaS APIs. Your MCP server is an App in the same workspace,
and Apps cannot be made public or bypass single sign-on, so the shared service principal has to
hold `CAN_USE` on the app. That is mechanically sound but not a documented walkthrough. Test it
early, and use the `http_request` check in step 3c to isolate the connection from the agent.

**"Custom LLM" Agent Bricks is now labelled legacy.** If you follow the Day 3 course notes into
that flow, expect it to look different. Custom Agents is the current path for a single
tool-calling agent, and the supervisor agent commands above are the CLI route.

**No documented ceiling on tool count.** Eight tools is almost certainly fine, since MCP catalogs
are described as aggregating many more, but no doc states a limit, so this is an assumption
rather than a fact.
