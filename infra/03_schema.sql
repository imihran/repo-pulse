-- 03_schema.sql — full data model for RepoPulse.
-- Apply with: make migrate
-- Safe to re-run: IF NOT EXISTS + ON CONFLICT DO NOTHING throughout.
--
-- Tables in dependency order (each table only references tables above it):
--   repos (already exists)
--   events (already exists)
--   repo_metrics_daily
--   github_artifacts
--   artifact_chunks
--   anomalies
--   investigation_reports
--   report_citations


-- ── repo_metrics_daily ────────────────────────────────────────────────────────
-- One row per repo per day. The anomaly detector (Slice 4) reads this table;
-- the ingest script populates it by aggregating the raw events table.
-- Keeping aggregates here means the detector never does a full GROUP BY over
-- millions of events at query time — it just scans this small summary table.

CREATE TABLE IF NOT EXISTS repo_metrics_daily (
    id                 SERIAL PRIMARY KEY,
    repo_name          TEXT NOT NULL REFERENCES repos(name),
    date               DATE NOT NULL,

    star_count         INT NOT NULL DEFAULT 0,  -- WatchEvent count
    fork_count         INT NOT NULL DEFAULT 0,  -- ForkEvent count
    issue_opened       INT NOT NULL DEFAULT 0,  -- IssuesEvent where action='opened'
    issue_closed       INT NOT NULL DEFAULT 0,  -- IssuesEvent where action='closed'
    pr_opened          INT NOT NULL DEFAULT 0,  -- PullRequestEvent where action='opened'
    pr_merged          INT NOT NULL DEFAULT 0,  -- PullRequestEvent where merged=true
    pr_avg_merge_hours FLOAT,                   -- NULL if no merges that day
    commit_count       INT NOT NULL DEFAULT 0,  -- PushEvent count (proxy for commits)

    -- UNIQUE enforces one row per repo per day AND creates an implicit index,
    -- which makes the common query pattern (WHERE repo_name=X AND date BETWEEN ...)
    -- fast without a separate CREATE INDEX.
    UNIQUE (repo_name, date)
);


-- ── github_artifacts ──────────────────────────────────────────────────────────
-- Issues and PRs fetched from the GitHub API for text enrichment.
-- GH Archive gives us event counts; GitHub API gives us the actual text
-- (title, body, comments) that the investigator agent reads.
-- Populated in Slice 5 (enrichment + embed).

CREATE TABLE IF NOT EXISTS github_artifacts (
    id           SERIAL PRIMARY KEY,
    repo_name    TEXT NOT NULL REFERENCES repos(name),

    -- 'issue' or 'pull_request' — CHECK enforces the enum at the DB level
    -- so application bugs that pass a typo are caught immediately.
    type         TEXT NOT NULL CHECK (type IN ('issue', 'pull_request')),

    number       INT  NOT NULL,       -- GitHub issue/PR number (e.g. 1234)
    title        TEXT,
    body         TEXT,                -- full description body
    state        TEXT,                -- 'open' or 'closed'
    author_login TEXT,
    created_at   TIMESTAMPTZ,
    closed_at    TIMESTAMPTZ,         -- NULL if still open
    labels       JSONB,               -- e.g. ["bug", "good first issue"]
    url          TEXT,                -- canonical GitHub URL, used in citations

    -- Track when we last fetched this so we know when to re-fetch for staleness.
    fetched_at   TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- An issue and a PR can share the same number in GitHub — they're different
    -- objects. The UNIQUE includes type to handle that correctly.
    UNIQUE (repo_name, type, number)
);


-- ── artifact_chunks ───────────────────────────────────────────────────────────
-- Each artifact is split into overlapping text chunks for embedding.
-- Chunking is necessary because embedding models have token limits (~8k);
-- a long issue with many comments must be split before we can embed it.
-- The embedding column is NULL until Slice 5 runs the embedding model.

CREATE TABLE IF NOT EXISTS artifact_chunks (
    id            SERIAL PRIMARY KEY,
    artifact_id   INT  NOT NULL REFERENCES github_artifacts(id),
    chunk_index   INT  NOT NULL,   -- 0-indexed position within the artifact

    text          TEXT NOT NULL,   -- the raw text of this chunk
    embedding     vector(1536),    -- 1536 dims = OpenAI text-embedding-3-small
                                   -- NULL until Slice 5 populates it

    -- These three columns are denormalized from github_artifacts.
    -- Redundant, but avoids a JOIN on every vector similarity query,
    -- which matters because pgvector scans the index and then fetches rows.
    repo_name     TEXT NOT NULL,
    artifact_type TEXT NOT NULL,
    artifact_url  TEXT,            -- copied here so citations don't need a join

    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),

    UNIQUE (artifact_id, chunk_index)
);

-- Vector similarity index — created AFTER Slice 5 loads embeddings.
-- HNSW (Hierarchical Navigable Small World) is the right choice here:
--   - Can be created at any time (unlike IVFFlat which needs training data)
--   - Better recall than IVFFlat at similar speed
--   - cosine distance (<=> operator) is correct for normalized embeddings
-- Run this manually after embedding is done:
--   CREATE INDEX ON artifact_chunks USING hnsw (embedding vector_cosine_ops);


-- ── anomalies ─────────────────────────────────────────────────────────────────
-- One row per detected anomaly. The detector (Slice 4) writes here;
-- the investigator agent (Slice 6) reads here and updates status.
-- The three MVP anomaly types:
--   issue_spike   — unusually high issue-open rate
--   pr_slowdown   — unusually long PR merge time
--   star_spike    — unusually high star/watch rate

CREATE TABLE IF NOT EXISTS anomalies (
    id             SERIAL PRIMARY KEY,
    repo_name      TEXT NOT NULL REFERENCES repos(name),

    -- One of the three MVP types. CHECK enforces valid values.
    anomaly_type   TEXT NOT NULL CHECK (anomaly_type IN ('issue_spike', 'pr_slowdown', 'star_spike')),

    detected_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    window_start   DATE NOT NULL,   -- first day of the 7-day observation window
    window_end     DATE NOT NULL,   -- last day (window_end - window_start = 6 days)

    -- Which metric triggered and what values it had.
    -- Storing both the metric and raw values lets us explain the anomaly
    -- without re-querying repo_metrics_daily.
    metric_name    TEXT  NOT NULL,   -- e.g. 'issue_opened', 'pr_avg_merge_hours'
    baseline_value FLOAT NOT NULL,   -- prior-28d average (the "normal" baseline)
    observed_value FLOAT NOT NULL,   -- last-7d average (what we actually saw)
    z_score        FLOAT NOT NULL,   -- MAD z-score: how many deviations above baseline

    -- Lifecycle: pending → investigating → done
    status         TEXT NOT NULL DEFAULT 'pending'
                   CHECK (status IN ('pending', 'investigating', 'done'))
);

-- The detector and agent query anomalies by status and recency.
CREATE INDEX IF NOT EXISTS anomalies_status_detected
    ON anomalies (status, detected_at DESC);


-- ── investigation_reports ─────────────────────────────────────────────────────
-- One report per investigated anomaly. Written by the LangGraph agent (Slice 6).
-- The output contract (summary, root_cause, confidence, limitations) matches
-- the Pydantic schema defined in the agent — we store both parsed fields AND
-- the raw JSON for debugging and Langfuse trace correlation.

CREATE TABLE IF NOT EXISTS investigation_reports (
    id               SERIAL PRIMARY KEY,
    anomaly_id       INT  NOT NULL REFERENCES anomalies(id),

    -- Human-readable findings (extracted from the agent's strict JSON output)
    summary          TEXT,
    root_cause       TEXT,
    confidence       TEXT CHECK (confidence IN ('high', 'medium', 'low')),
    limitations      TEXT,    -- what the agent couldn't determine (always document this)

    -- Cost and performance tracking — feeds the observability dashboard (Slice 8)
    tool_calls_used  INT,
    tokens_used      INT,
    cost_usd         FLOAT,
    duration_seconds FLOAT,

    -- Full structured output from the agent, stored for debugging.
    -- Langfuse trace ID will live here too, once wired in Slice 8.
    raw_output       JSONB,

    created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);


-- ── report_citations ──────────────────────────────────────────────────────────
-- Every factual claim in a report must map to a citation.
-- This is the "cited-outputs-only" guardrail from the spec:
-- the agent is not allowed to assert something it can't point at.
-- Three citation types:
--   sql_result     — came from a run_sql() tool call (structured evidence)
--   artifact_chunk — came from a semantic search hit (unstructured evidence)
--   release_note   — came from get_release_notes() tool call

CREATE TABLE IF NOT EXISTS report_citations (
    id            SERIAL PRIMARY KEY,
    report_id     INT  NOT NULL REFERENCES investigation_reports(id),

    citation_type TEXT NOT NULL
                  CHECK (citation_type IN ('sql_result', 'artifact_chunk', 'release_note')),

    source_url    TEXT,   -- GitHub URL or a description like 'SQL: SELECT ...'
    excerpt       TEXT,   -- the specific passage or value that was cited

    -- Only set for artifact_chunk citations — lets us link back to the chunk
    -- and re-retrieve it if needed. NULL for sql_result and release_note.
    artifact_id   INT REFERENCES github_artifacts(id)
);

-- Reports typically retrieve all their citations at once for rendering.
CREATE INDEX IF NOT EXISTS report_citations_report_id
    ON report_citations (report_id);
