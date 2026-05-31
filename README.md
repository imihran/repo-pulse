# RepoPulse

> **Datadog-style incident investigation, but for open-source repo health.**
> An autonomous agent that monitors GitHub repositories, detects meaningful changes in their activity, and produces **cited, root-cause explanations** of *why* each change happened.

**Status:** core detect → investigate → explain loop is working end-to-end. Slices 1–7 complete.

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
| Evaluation | Custom golden benchmark (10 cases) | Citation coverage, budget compliance, latency, groundedness |
| Local infra | **docker-compose** | One command to start; cloud-portable by design |

---

## Project structure

```
repo-pulse/
├── docker-compose.yml          local Postgres + pgvector
├── Makefile                    all commands: make up / download / process / detect / investigate / eval
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
│   └── agent.py                LangGraph investigator: SQL + semantic + release tools
│
└── eval/
    ├── golden_cases.json        10-case benchmark with expected evidence URLs
    ├── prepare.py               set up anomaly rows, enrich, embed all benchmark windows
    └── evaluator.py             run agent on benchmark; score citation coverage, latency, budget
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

**Known gap:** citation coverage is low because the agent cites SQL results by description (not URL) and the expected URLs point to PRs opened before the enrichment window. Fix tracked in Slice 8.

---

## What's next

| Slice | Description |
|---|---|
| 8 | **Observability + guardrails** — Langfuse traces, prompt-injection tests, read-only SQL enforcement, cited-outputs-only validation |
| 9 | **Public demo** — feed of precomputed reports; report detail with citations + agent trace |
| 10 | **Harden** — add Airflow for scheduled ingestion; expand golden set; hybrid (BM25) retrieval |
| 11 | **Launch** — investigate marquee repos; publish first health reports |

---

## Design decisions (key ones)

- **One Postgres instead of a separate vector DB** — pgvector keeps ops simple and enables SQL JOINs between structured metrics and semantic search results in a single query. At our scale (thousands of chunks), there's no performance penalty.
- **Median not mean for PR merge time** — mean is wrecked by a single 300-day-old PR merging in a quiet week (confirmed by Codex analysis on real data). Median is robust.
- **MAD z-score not plain z-score** — a single spike in the prior 28d baseline inflates the mean and suppresses future detection. MAD uses the median as its center.
- **LangGraph over CrewAI** — explicit state machine with visible transitions. Every tool call, observation, and routing decision is inspectable. CrewAI abstracts this away.
- **Download-then-process, not stream-only** — downloading raw GH Archive files to disk first decouples network failures from processing failures. A crash mid-process restarts from disk without re-downloading.

---

*See [`PROJECT.md`](./PROJECT.md) for the full engineering spec.*
