-- 02_schema.sql — Slice 2 minimal schema.
-- Apply with: make migrate
-- Safe to re-run: every statement uses IF NOT EXISTS or ON CONFLICT DO NOTHING.

-- The 3 repos we track. Everything else in the schema references this table.
CREATE TABLE IF NOT EXISTS repos (
    id   SERIAL PRIMARY KEY,
    name TEXT NOT NULL UNIQUE  -- 'owner/repo' format, e.g. 'vllm-project/vllm'
);

-- Seed the MVP repos. ON CONFLICT DO NOTHING makes this idempotent.
INSERT INTO repos (name) VALUES
    ('vllm-project/vllm'),
    ('langchain-ai/langchain'),
    ('dbt-labs/dbt-core')
ON CONFLICT (name) DO NOTHING;

-- Raw events from GH Archive, one row per event.
-- We keep the full payload as JSONB so we can query into it later without
-- knowing the exact shape of every event type upfront.
CREATE TABLE IF NOT EXISTS events (
    id           TEXT PRIMARY KEY,       -- GH event ID (globally unique string)
    type         TEXT NOT NULL,          -- WatchEvent, ForkEvent, PushEvent, etc.
    repo_name    TEXT NOT NULL
                 REFERENCES repos(name), -- FK enforces we only store watched repos
    actor_login  TEXT,                   -- GitHub username who triggered the event
    created_at   TIMESTAMPTZ NOT NULL,
    payload      JSONB                   -- full event-specific payload for later slices
);

-- Composite index: almost every query will filter by repo and sort/filter by time.
CREATE INDEX IF NOT EXISTS events_repo_created
    ON events (repo_name, created_at DESC);
