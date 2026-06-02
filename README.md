# RepoPulse

> **Datadog-style incident investigation, but for open-source repo health.**
> An autonomous agent that monitors GitHub repositories, detects meaningful changes in their activity, and produces **cited, root-cause explanations** of *why* each change happened.

**Status:** Slices 0–10 complete ✅ · Live at **[repopulse.devmish.com](https://repopulse.devmish.com)**

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
  • Median merge hours spiked to 72h (baseline 6h, z=7.74)
  • PR #30300 open 1070h, PR #30444 open 890h, PR #30514 open 800h
  • PR #31060 "community: move to separate repo" merged Apr 29
  • Release langchain-core v0.3.56 on Apr 24 (via release notes)

Citations:
  [sql_result]      SQL: Median merge hours from April 24 to April 30
  [artifact_chunk]  https://github.com/langchain-ai/langchain/pull/30300
  [artifact_chunk]  https://github.com/langchain-ai/langchain/pull/31060
  [release_note]    langchain-core v0.3.56 — Apr 24 2025
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
- "Which PRs were slowest?" → agent calls **find_slow_prs** (queries `github_artifacts` by actual GitHub API timestamps — catches PRs opened before the event window)
- "Why / what did developers say?" → agent runs **hybrid semantic search** (vector + BM25 via RRF) over embedded PR/issue text
- "Did a release happen?" → agent calls **get_release_notes**

The agent loops — if evidence is weak it re-queries — and stops when it can write a grounded report or hits the budget (8 tool calls, 90s).

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
| Retrieval | **Hybrid BM25 + vector (RRF)** | Vector for semantic similarity; BM25 for exact keywords (PR numbers, names). GIN index on `artifact_chunks`. |
| Vector index | **HNSW** (pgvector) | Fast approximate nearest-neighbour, no retraining needed |
| Output validation | **Pydantic** | Strict JSON contract — agent can't produce uncited claims |
| Observability | **Langfuse** | Every agent step traced: tool calls, token counts, cost, latency |
| Guardrails | **pytest** (31 tests) | SQL injection blocked, prompt injection stopped at output contract, cited-only validation |
| Evaluation | Custom golden benchmark (15 cases) | Citation coverage, budget compliance, latency, confidence |
| Scheduling | **Airflow** (`dags/repopulse_daily.py`) | Daily pipeline: download → process → detect → enrich → embed → investigate → export |
| Local infra | **docker-compose** | One command to start; cloud-portable by design |
| Frontend | **Vanilla HTML/CSS/JS** | Static site — no build step, no framework, deploys to CDN instantly |
| Hosting | **Cloudflare Pages** | Free tier, global CDN, SSL, custom domain — `repopulse.devmish.com` |

---

## Project structure

```
repo-pulse/
├── docker-compose.yml          local Postgres + pgvector
├── Makefile                    all commands: make up / download / detect / eval / airflow-up …
├── pyproject.toml
├── .env.example                copy to .env and fill in tokens
│
├── infra/
│   ├── init.sql                activates pgvector extension on first boot
│   ├── 02_schema.sql           repos + events tables
│   ├── 03_schema.sql           full schema (metrics, artifacts, anomalies, reports, citations)
│   ├── 04_rename_pr_metric.sql migration: avg → median merge hours
│   └── 05_add_bm25_index.sql   GIN index for BM25/FTS hybrid retrieval
│
├── dags/
│   └── repopulse_daily.py      Airflow DAG: 7-task daily ingestion + investigation pipeline
│
├── repopulse/
│   ├── db.py                   Postgres connection factory
│   ├── manifest.py             download/ingest progress tracker
│   ├── downloader.py           fetch raw GH Archive .json.gz files; download_range() API
│   ├── processor.py            filter + upsert events from disk into Postgres; process_range() API
│   ├── detector.py             aggregate daily metrics; MAD z-score anomaly detection
│   ├── enricher.py             fetch PR/issue text from GitHub API → github_artifacts
│   │                           (GitHub search backfill for PRs opened before event window)
│   ├── embedder.py             chunk + embed artifacts → pgvector (artifact_chunks)
│   ├── agent.py                LangGraph investigator: run_sql / find_slow_prs /
│   │                           semantic_search (BM25+vector RRF) / get_release_notes
│   ├── export.py               export reports from DB → static JSON for Cloudflare Pages
│   └── observability.py        Langfuse callback handler (no-op if keys absent)
│
├── eval/
│   ├── golden_cases.json        15-case benchmark (pr_slowdown × 3 repos + star_spike)
│   ├── prepare.py               set up anomaly rows, enrich + embed all benchmark windows
│   └── evaluator.py             run agent on benchmark; score citation coverage, latency, budget
│
├── tests/
│   └── test_guardrails.py       31 guardrail tests (SQL safety, injection, output contract)
│
└── web/                         static frontend — deployed to Cloudflare Pages
    ├── index.html               incident-style feed of all investigations
    ├── report.html              full report detail with citations panel
    ├── assets/style.css         dark monitoring-tool aesthetic (Syne + JetBrains Mono)
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
make migrate                  # applies base schema
make migrate-bm25             # adds GIN index for BM25 hybrid retrieval
```

### 2. Download and ingest GH Archive data

```bash
# Step 1: download raw hourly files to data/raw/ (~100 MB each)
make download ARGS="--start 2025-03-01 --end 2025-04-30"

# Step 2: filter to the 3 watched repos and push into Postgres
make process ARGS="--start 2025-03-01 --end 2025-04-30 --delete-raw"
```

`--delete-raw` removes each raw file after processing to reclaim disk space.

### 3. Detect anomalies

```bash
make detect ARGS="--window-end 2025-04-30"
```

### 4. Enrich and embed the anomaly window

```bash
make enrich ARGS="--repo langchain-ai/langchain --start 2025-04-24 --end 2025-04-30"
make embed  ARGS="--repo langchain-ai/langchain"
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
make psql
# SELECT id, repo_name, anomaly_type, window_start, window_end, z_score FROM anomalies;
\q

make investigate ARGS="--anomaly-id 7"
```

### 6. Run the benchmark eval

```bash
make eval-prepare    # enrich + embed all 15 golden case windows
make eval            # run agent on all cases, print scores
make eval ARGS="--judge"           # add LLM-based groundedness scoring
make eval ARGS="--skip-existing"   # reuse stored reports (faster/cheaper)
```

### 7. Run the guardrail tests

```bash
make test            # 31 tests, no DB or LLM required
```

### 8. Start Airflow (scheduled daily pipeline)

```bash
source .venv/bin/activate
make airflow-init    # one-time: installs Airflow, migrates SQLite DB, creates admin user
make airflow-up      # starts scheduler + webserver → http://localhost:8080 (admin/admin)
```

The `repopulse_daily` DAG runs at 6 AM UTC and processes the previous day's data end-to-end. Toggle it on in the UI to activate the schedule. Trigger manually with:
```bash
make airflow-trigger DATE=2025-04-30
```

### 9. Deploy the public demo

```bash
make export          # export reports from DB to web/data/ (static JSON)
wrangler pages deploy ./web --project-name repopulse --branch main
```

Live at **https://repopulse.devmish.com**

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

## Eval results (15-case benchmark, June 2026)

| Metric | Score |
|---|---|
| Budget compliance (≤ 8 tool calls) | **15 / 15** |
| Latency compliance (< 90s) | **15 / 15** |
| Confidence match | **14 / 15** |
| Avg latency | **25.6s** |
| Avg citation coverage | **0.83** |

Citation coverage improved from 0.08 (baseline) to 0.83 after:
- Adding `find_slow_prs` tool querying `github_artifacts` (catches pre-window PRs)
- GitHub search API backfill in enricher for PRs merged but opened before event window
- Mandatory semantic search step in investigation sequence
- `generate_report` retry on validation failure

---

## How it was built — slice by slice (plain English)

**Slice 1 — Turn on the database**
Before writing a single line of application code, we set up a local database that can store both regular data (event counts, dates, numbers) and AI vectors (the mathematical representations of text). One command starts the whole thing.

**Slice 2 — Get real data in**
GitHub publishes a record of every public action on the platform — every star, comment, pull request, and code push — as compressed files you can download. We wrote two scripts: one that downloads those files to disk, and one that reads them and keeps only the events for our three watched repos. 60 days of data, 33,563 events.

**Slice 3 — Design the full data model**
Before building the intelligence layer, we designed all the tables the system would ever need: raw events, daily summaries, issue and PR text, text chunks for AI search, detected anomalies, investigation reports, and citations. Getting this right upfront means no schema rewrites later.

**Slice 4 — Spot unusual changes automatically**
Every day, the system compares the last 7 days of activity against the prior 28 days. If something is statistically unusual — PRs taking much longer to merge, a spike in issues, a surge in stars — it gets flagged. We use a robust statistical method (MAD z-score) that isn't fooled by a single weird day in the history. We also discovered through data analysis that using the *median* merge time (not the average) prevents one very old PR from triggering a false alarm.

**Slice 5 — Make the text searchable by meaning**
Pull requests and issues are written in natural language. To let the AI ask questions like "what were developers complaining about this week?", we fetch the full text of each PR (title, description, comments) from GitHub, chop it into chunks, and convert each chunk into a mathematical vector using OpenAI's embedding model. We also add a BM25 full-text index so exact keywords (PR numbers, error names) are found even when the semantic embedding doesn't rank them highly. Both results are fused via Reciprocal Rank Fusion.

**Slice 6 — The investigator agent**
This is the centrepiece. When an anomaly is detected, the agent has four tools: `run_sql` (counts and trends), `find_slow_prs` (queries GitHub API timestamps to find the exact outlier PRs — even ones opened before our event window), `semantic_search` (hybrid BM25+vector search over PR/issue text), and `get_release_notes` (for spotting if a new version caused the change). The agent follows a structured sequence — confirm metrics, find specific artifacts, search for why — and stops when it can write a grounded report or hits the budget (8 tool calls, 90s).

**Slice 7 — Check if the agent is actually right**
We built a benchmark of 15 hand-crafted test cases — situations where we already know what happened and what evidence the agent should find. The evaluator runs the agent on every case and scores it: Did it cite the right pull requests? Did it stay within budget? Did it finish in time? Did it pick the right confidence level?

**Slice 8 — Make it trustworthy**
Two things need to be true before this system can be trusted in production: we need to see exactly what it's doing (every LLM call, token count, and cost), and we need to know it can't be hijacked by malicious content in a PR. Langfuse wires into every investigation and produces a full trace. 31 automated tests verify SQL injection is blocked, prompt injection is stopped, and the agent can never produce a report without citations.

**Slice 9 — Put it on the internet**
The pipeline produces reports, but they only lived in a local database. A Python export script reads every investigation and writes it as static JSON. A hand-built frontend — dark monitoring-tool aesthetic, incident-style feed cards, full citation panel — loads those files and renders them. Deployed to Cloudflare Pages under `repopulse.devmish.com`. No live backend needed.

**Slice 10 — Harden**
Four major improvements: (1) Citation coverage fixed from 0.08 → 0.83 by adding a dedicated `find_slow_prs` tool and GitHub search API backfill for pre-window PRs. (2) Hybrid BM25+vector retrieval using PostgreSQL FTS fused with vector search via RRF. (3) Golden benchmark expanded from 10 to 15 cases including the first `star_spike` type. (4) Airflow daily pipeline (`dags/repopulse_daily.py`) with a 7-task dependency chain that runs automatically each morning.

---

## What's next

| Slice | Description |
|---|---|
| 11 | **Launch** — investigate marquee repos with fresh data; publish health reports; Show HN post |

---

## Design decisions (key ones)

- **One Postgres instead of a separate vector DB** — pgvector keeps ops simple and enables SQL JOINs between structured metrics and semantic search results in a single query.
- **Hybrid BM25 + vector retrieval** — vector search alone misses exact keyword matches (PR numbers, error names, package names). BM25 catches those; RRF combines both ranked lists without needing a reranking model.
- **`find_slow_prs` tool using `github_artifacts`** — GH Archive self-joins only work for PRs opened within the event window. Many anomalies are caused by PRs open for months. The GitHub API data in `github_artifacts` has the actual creation timestamps.
- **Median not mean for PR merge time** — mean is wrecked by a single 300-day-old PR merging in a quiet week (confirmed by data analysis). Median is robust.
- **MAD z-score not plain z-score** — a single spike in the prior 28d baseline inflates the mean and suppresses future detection. MAD uses the median as its center.
- **LangGraph over CrewAI** — explicit state machine with visible transitions. Every tool call, observation, and routing decision is inspectable.
- **Download-then-process, not stream-only** — decouples network failures from processing failures. A crash mid-process restarts from disk without re-downloading.
- **Airflow standalone (same venv)** — simpler than a separate Docker container; the DAG imports `repopulse` directly. Absolute paths in all modules (`Path(__file__).resolve().parent.parent`) so tasks work from any working directory.

---

*See [`PROJECT.md`](./PROJECT.md) for the full engineering spec.*
