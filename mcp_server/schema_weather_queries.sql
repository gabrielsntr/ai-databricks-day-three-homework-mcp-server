-- Query log table schema for the weather MCP server.
-- Run this SQL against your Lakebase Postgres database to create the table
-- that query_log.py writes to and the dashboard reads from.

CREATE TABLE IF NOT EXISTS weather_queries (
    id            BIGSERIAL PRIMARY KEY,
    tool_name     TEXT        NOT NULL,
    location_query TEXT,
    resolved_label TEXT,
    latitude      DOUBLE PRECISION,
    longitude     DOUBLE PRECISION,
    status        TEXT        NOT NULL,
    verdict       TEXT,
    summary       TEXT,
    duration_ms   INTEGER,
    requested_by  TEXT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS weather_queries_created_at_idx
    ON weather_queries (created_at DESC);
