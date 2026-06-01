# RepoPulse

> **Datadog-style incident investigation, but for open-source repo health.**
> An autonomous agent that monitors GitHub repositories, detects meaningful changes in their activity, and produces **cited, root-cause explanations** of *why* each change happened.

**Status:** fully deployed. Slices 1–9 complete. Live at **[repopulse.pages.dev](https://repopulse.pages.dev)** → **[repopulse.devmish.com](https://repopulse.devmish.com)** *(custom domain propagating)*

---

## What it does

Most GitHub analytics tools answer questions you already know to ask. RepoPulse runs continuously, **spots unusual changes on its own**, and then acts like an on-call analyst — digging through recent activity and writing a short cited explanation of what happened.

**Real example** — detected on `langchain-ai/langchain`, window Apr 24–30 2025:

```
Anomaly:    pr_slowdown
Metric:     pr_median_merge_hours
Baseline:   6.0h  (prior 28-day median)
Observed:   72.0h (last 7-day median)
Z-score:    7.74

Summary: Significant PR merge slowdown coinciding with the release of
langchain-core v0.3.56 and an active repository restructuring effort
(langchain-community moving to a separate repo).

Root cause: Repository restructuring (langchain-community moving to a
separate repo) combined with concurrent release of v0.3.56 increased
review complexity and slowed PR merges during this window.

Confidence: MEDIUM

Evidence:
  • Median merge hours spiked to 333h on April 28 (SQL)
  • Release of langchain-core v0.3.56 on April 24 introduced multiple changes
  • PR #31069 "community: move to separate repo" — restructuring in progress

Citations:
  [sql_result]      SQL: Median merge hours data
  [release_note]    https://github.com/langchain-ai/langchain/releases/tag/v0.3.56
  [artifact_chunk]  https://github.com/langchain-ai/langchain/pull/31069
```

---

## How it works

```
  GH Archive ──┐
               ├──▶  Postgres + pgvector  ──▶  Anomaly detector  ──▶  Investigator agent
  GitHub API ──┘     (events, metrics,         (last 7d vs             (LangGraph loop)
                      embeddings)               prior 28d,                   │
                                                MAD z-score)                 ▼
                                                                       Cited report
                                                                       + confidence
                                                                       + limitations
```

**The routing rule** (what makes the agent non-trivial):
- "How many / trend / average?" → agent runs **SQL** against the metrics tables
- "Why / what did developers say?" → agent runs **semantic search** over embedded PR/issue text
- "Did a release happen?" → agent calls **get_release_notes**

The agent loops — if evidence is weak it re-queries — and stops when it can write a grounded report or hits the budget (6 tool calls, 90s).

---

## Tech stack

| Layer | Tool | Why |
|---|---|---|
| Data source | **GH Archive** + **GitHub REST API** | Hourly event firehose + PR/issue text |
| Storage | **Postgres 16 + pgvector 0.8** | One database for structured events and vector embeddings |
| Anomaly detection | **MAD z-score** (last 7d vs prior 28d) | Robust to outliers; median merge time not mean |
| Agent framework | **LangGraph** | Explicit, debuggable state machine — every transition visible |
| LLM | **GPT-4o-mini** (OpenAI) | ~$0.01–0.02 per investigation, well within $0.25 budget |
| Embeddings | **text-embedding-3-small** (1536 dims) | Semantic search over PR/issue bodies + comments |
| Vector index | **HNSW** (pgvector) | Fast approximate nearest-neighbour, no retraining needed |
| Output validation | **Pydantic** | Strict JSON contract — agent can't produce uncited claims |
| Observability | **Langfuse** | Every agent step traced: tool calls, token counts, cost, latency |
| Guardrails | **pytest** (31 tests) | SQL injection blocked, prompt injection stopped at output contract, cited-only validation |
| Evaluation | Custom golden benchmark (10 cases) | Citation coverage, budget compliance, latency, groundedness |
| Local infra | **docker-compose** | One command to start; cloud-portable by design |
| Frontend | **Vanilla HTML/CSS/JS** | Static site — no build step, no framework, deploys to CDN instantly |
| Hosting | **Cloudflare Pages** | Free tier, global CDN, SSL, custom domain — `repopulse.devmish.com` |

---

## Project structure

```
repo-pulse/
├── docker-compose.yml          local Postgres + pgvector
├── Makefile                    all commands: make up / download / process / detect / investigate / eval / export
├── pyproject.toml
├── .env.example                copy to .env and fill in tokens
│
├── infra/
│   ├── init.sql                activates pgvector extension on first boot
│   ├── 02_schema.sql           repos + events tables
│   ├── 03_schema.sql           full schema (metrics, artifacts, anomalies, reports, citations)
│   └── 04_rename_pr_metric.sql migration: avg → median merge hours
│
├── repopulse/
│   ├── db.py                   Postgres connection factory
│   ├── manifest.py             download/ingest progress tracker
│   ├── downloader.py           fetch raw GH Archive .json.gz files to disk
│   ├── processor.py            filter + upsert events from disk into Postgres
│   ├── detector.py             aggregate daily metrics; MAD z-score anomaly detection
│   ├── enricher.py             fetch PR/issue text from GitHub API → github_artifacts
│   ├── embedder.py             chunk + embed artifacts → pgvector (artifact_chunks)
│   ├── agent.py                LangGraph investigator: SQL + semantic + release tools
│   ├── export.py               export reports from DB → static JSON for Cloudflare Pages
│   └── observability.py        Langfuse callback handler (no-op if keys absent)
│
├── eval/
│   ├── golden_cases.json        10-case benchmark with expected evidence URLs
│   ├── prepare.py               set up anomaly rows, enrich, embed all benchmark windows
│   └── evaluator.py             run agent on benchmark; score citation coverage, latency, budget
│
├── tests/
│   └── test_guardrails.py       31 guardrail tests (SQL safety, injection, output contract)
│
└── web/                         static frontend — deployed to Cloudflare Pages
    ├── index.html               incident-style feed of all investigations
    ├── report.html              full report detail with citations panel
    ├── _headers                 Cloudflare security headers
    ├── _redirects               URL routing
    ├── assets/
    │   └── style.css            dark monitoring-tool aesthetic (Syne + JetBrains Mono)
    └── data/                    pre-generated JSON (regenerate with `make export`)
        ├── reports.json         feed index
        └── reports/{id}.json    individual report detail
```

---

## Run it yourself

### Prerequisites

- Docker + Docker Compose
- Python 3.10+
- GitHub personal access token ([create one](https://github.com/settings/tokens) — `repo` read scope)
- OpenAI API key

### 1. Start the database

```bash
git clone https://github.com/your-username/repo-pulse
cd repo-pulse
cp .env.example .env          # fill in GITHUB_TOKEN and OPENAI_API_KEY
make up                       # starts Postgres + pgvector on port 5433
make migrate                  # applies schema (runs infra/02_schema.sql + 03_schema.sql)
```

### 2. Download and ingest GH Archive data

```bash
# Step 1: download raw hourly files to data/raw/ (~100 MB each)
make download ARGS="--start 2025-03-01 --end 2025-04-30"

# Step 2: filter to the 3 watched repos and push into Postgres
make process ARGS="--start 2025-03-01 --end 2025-04-30 --delete-raw"
```

`--delete-raw` removes each raw file after processing to reclaim disk space (~100 GB for 60 days). Skip it if you want to keep the raw files for re-processing.

### 3. Detect anomalies

```bash
make detect ARGS="--window-end 2025-04-30"
```

Aggregates raw events into daily metrics, computes MAD z-scores (last 7d vs prior 28d), and writes anomaly rows to the `anomalies` table.

### 4. Enrich and embed the anomaly window

```bash
# Fetch PR text from GitHub API for the anomaly window
make enrich ARGS="--repo langchain-ai/langchain --start 2025-04-24 --end 2025-04-30"

# Chunk + embed into pgvector
make embed ARGS="--repo langchain-ai/langchain"
```

Then build the HNSW vector index:
```bash
make psql
# inside psql:
CREATE INDEX ON artifact_chunks USING hnsw (embedding vector_cosine_ops);
\q
```

### 5. Investigate an anomaly

```bash
# List anomalies
make psql
SELECT id, repo_name, anomaly_type, window_start, window_end, z_score FROM anomalies;
\q

# Run the agent
make investigate ARGS="--anomaly-id 7"
```

### 6. Run the benchmark eval

```bash
make eval-prepare    # enrich + embed all 10 golden case windows
make eval            # run agent on all cases, print scores
make eval ARGS="--judge"   # add LLM-based groundedness scoring
```

### 7. Run the guardrail tests

```bash
make test            # 31 tests, no DB or LLM required
```

### 8. Deploy the public demo

```bash
make export          # export reports from DB to web/data/ (static JSON)
wrangler pages deploy ./web --project-name repopulse --branch main
```

Live at **https://repopulse.pages.dev** · Custom domain: **https://repopulse.devmish.com**

---

## Repos tracked (MVP)

| Repo | Why |
|---|---|
| `vllm-project/vllm` | High-velocity AI infra project — lots of activity, clear anomaly signals |
| `langchain-ai/langchain` | Active restructuring (community split) — real detectable events |
| `dbt-labs/dbt-core` | Lower-volume contrast — tests noise suppression (min_baseline guard) |

## Anomaly types (MVP)

| Type | Metric | Detection |
|---|---|---|
| `pr_slowdown` | `pr_median_merge_hours` | Median time from PR open to merge (robust to stale-PR outliers) |
| `issue_spike` | `issue_opened` | Daily issue-open rate |
| `star_spike` | `star_count` | Daily WatchEvent count |

---

## Eval results (10-case benchmark, May 2026)

| Metric | Score |
|---|---|
| Budget compliance (≤ 6 tool calls) | **10 / 10** |
| Latency compliance (< 90s) | **10 / 10** |
| Confidence match | **10 / 10** |
| Avg latency | **13.6s** |
| Avg citation coverage | 0.08 *(known gap — see below)* |

**Known gap:** citation coverage is low because the agent cites SQL results by description (not URL) and the expected URLs point to PRs opened before the enrichment window. Fix tracked in Slice 10.

---

## How it was built — slice by slice (plain English)

The project was built in 11 incremental slices. Each slice had to be working and explainable before the next one started.

**Slice 1 — Turn on the database**
Before writing a single line of application code, we set up a local database that can store both regular data (event counts, dates, numbers) and AI vectors (the mathematical representations of text). One command starts the whole thing.

**Slice 2 — Get real data in**
GitHub publishes a record of every public action on the platform — every star, comment, pull request, and code push — as compressed files you can download. We wrote two scripts: one that downloads those files to disk, and one that reads them and keeps only the events for our three watched repos. 60 days of data, 33,563 events.

**Slice 3 — Design the full data model**
Before building the intelligence layer, we designed all the tables the system would ever need: raw events, daily summaries, issue and PR text, text chunks for AI search, detected anomalies, investigation reports, and citations. Getting this right upfront means no schema rewrites later.

**Slice 4 — Spot unusual changes automatically**
Every day, the system compares the last 7 days of activity against the prior 28 days. If something is statistically unusual — PRs taking much longer to merge, a spike in issues, a surge in stars — it gets flagged. We use a robust statistical method (MAD z-score) that isn't fooled by a single weird day in the history. We also discovered through data analysis that using the *median* merge time (not the average) prevents one very old PR from triggering a false alarm.

**Slice 5 — Make the text searchable by meaning**
Pull requests and issues are written in natural language. To let the AI ask questions like "what were developers complaining about this week?", we fetch the full text of each PR (title, description, comments) from GitHub, chop it into chunks, and convert each chunk into a mathematical vector using OpenAI's embedding model. These vectors are stored in the database and let us find semantically similar content without exact keyword matching.

**Slice 6 — The investigator agent**
This is the centrepiece. When an anomaly is detected, the agent is given three tools: one to run database queries (for counts and trends), one to search PR and issue text by meaning (for understanding why), and one to check release notes (for spotting if a new version caused the change). The agent decides which tool to use at each step, runs it, reads the result, and decides whether it has enough evidence to write a report — or whether it needs to dig deeper. It has a budget of 6 tool calls and 90 seconds.

**Slice 7 — Check if the agent is actually right**
We built a benchmark of 10 hand-crafted test cases — situations where we already know what happened and what evidence the agent should find. The evaluator runs the agent on every case and scores it: Did it cite the right pull requests? Did it stay within budget? Did it finish in time? Did it pick the right confidence level? This gives us a baseline we can track as we improve the system.

**Slice 8 — Make it trustworthy**
Two things need to be true before this system can be trusted in production: we need to see exactly what it's doing (every LLM call, token count, and cost), and we need to know it can't be hijacked by malicious content in a PR. This slice wires in Langfuse — every investigation now produces a full trace visible in a dashboard showing each tool call, how long it took, and what it cost. We also added a guardrail test suite: 31 automated tests verify that the agent can't be manipulated by prompt injection attacks, can't run destructive SQL, and can never produce a report that makes claims without citing evidence.

**Slice 9 — Put it on the internet**
The pipeline produces reports, but they only lived in a local database. This slice exposes them publicly. A Python export script reads every investigation from the database and writes it as a static JSON file. A hand-built frontend — dark monitoring-tool aesthetic, incident-style feed cards, full citation panel — loads those files and renders them. The whole thing is deployed to Cloudflare Pages under a custom subdomain. No live backend needed: the reports are pre-generated and the site is pure static HTML/CSS/JS served from a global CDN. Consultation with Codex confirmed this was the right architecture: standalone subdomain, static JSON feed, Cloudflare Pages — not GitHub Pages, not a portfolio section.

---

## What's next

| Slice | Description |
|---|---|
| 10 | **Harden** — ingest current data (last 35 days, not year-old data); add Airflow for scheduled ingestion; improve search quality; expand the benchmark |
| 11 | **Launch** — investigate marquee repos with fresh data; publish health reports; post on HN |

---

## Design decisions (key ones)

- **One Postgres instead of a separate vector DB** — pgvector keeps ops simple and enables SQL JOINs between structured metrics and semantic search results in a single query. At our scale (thousands of chunks), there's no performance penalty.
- **Median not mean for PR merge time** — mean is wrecked by a single 300-day-old PR merging in a quiet week (confirmed by Codex analysis on real data). Median is robust.
- **MAD z-score not plain z-score** — a single spike in the prior 28d baseline inflates the mean and suppresses future detection. MAD uses the median as its center.
- **LangGraph over CrewAI** — explicit state machine with visible transitions. Every tool call, observation, and routing decision is inspectable. CrewAI abstracts this away.
- **Download-then-process, not stream-only** — downloading raw GH Archive files to disk first decouples network failures from processing failures. A crash mid-process restarts from disk without re-downloading.

---

*See [`PROJECT.md`](./PROJECT.md) for the full engineering spec.*
