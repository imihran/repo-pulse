# RepoPulse — Engineering Spec & Build Plan

This is the working source of truth for the project: what we're building, how it's
architected, and the slice-by-slice plan. The [`README.md`](./README.md) is the
plain-English overview; this doc is the detail. Living document — update as decisions change.

---

## 1. Mission & positioning

**Mission:** an autonomous agent that monitors open-source repositories, detects meaningful
changes in their health/activity, and produces **cited, root-cause explanations** of *why*
each change happened.

**One-line positioning:** *"Datadog-style incident investigation, but for open-source
repo health."*

**Why this framing (not "agentic RAG"):** the phrase "agentic RAG" labels a saturated
category of tutorial projects. The defensible, memorable hook is the **autonomous
investigation loop** — a system that fires on its own (no human query required), reasons
across structured + unstructured evidence, and explains itself with citations.

## 2. Goals & non-goals

**Goals**
- Detect anomalies in repo activity automatically (scheduled, not on-demand).
- Investigate each anomaly by fusing **structured queries (SQL)** with **semantic search**
  over developer text (issues/PRs/releases).
- Produce a concise explanation with a **confidence level** and **inline citations** to the
  exact evidence used.
- Be **live and publicly demoable** — type a repo, watch the agent work.
- Ship with a **production-grade reliability layer**: evaluation, observability, guardrails.

**Non-goals (explicitly out of scope)**
- Not a general "chat with GitHub" tool (OSS Insight already does NL→SQL well).
- Not fine-tuning / model training — we use hosted/off-the-shelf LLMs.
- Not a polished SaaS product — it's a flagship demonstrating end-to-end capability.
- Ingestion is **deliberately "boring and production-grade"** — it is not where the
  novelty lives, so we don't over-invest there.

## 3. Differentiation (vs. the landscape)

| Existing | What it does | What it does NOT do |
|---|---|---|
| **OSS Insight** (PingCAP) | One-shot natural-language → SQL over GH Archive; dashboards, rankings | No semantic layer over issue/PR *text*; no autonomous, multi-step investigation; no anomaly explanation |
| GH Archive / BigQuery examples | Raw queryable event data | No analysis, no agent |
| Generic agentic-RAG repos | Chatbot-style retrieve-then-answer | Not domain-specific; no autonomous trigger; no eval/obs as standard |

**Our defensible gap:** structured-event analytics **+** semantic retrieval over developer
text **+** autonomous multi-step root-cause investigation, in one system, running live.

## 4. Architecture

```
Sources         Storage              Intelligence              Serving
───────         ───────              ────────────              ───────
GH Archive ─┐   Postgres (events,    Anomaly detector  ─┐      FastAPI
            ├──▶ metrics, time series)     │            │      Streamlit/Gradio demo
GitHub API ─┘   pgvector (issue/PR    Investigator agent│      Public "health report" feed
                 text embeddings)      (LangGraph):      │
                                        ├ SQL tool       │
                                        ├ semantic tool  │
                                        ├ release reader │
                                        └ loop/reflect ──┘
                                       Report + citations

Cross-cutting: Eval (RAGAS + golden set + CI gate) · Observability (Langfuse) ·
               Guardrails (injection tests, tool allowlist, cited-outputs-only)
```

## 5. Data sources

- **GH Archive** (`gharchive.org`) — hourly JSON of all public GitHub events since 2011.
  Primary firehose. Free, downloadable, incremental by the hour. Key event types:
  `WatchEvent` (stars), `ForkEvent`, `PushEvent`, `PullRequestEvent`, `IssuesEvent`,
  `IssueCommentEvent`, `ReleaseEvent`, `CreateEvent`.
- **GitHub REST/GraphQL API** — enrichment: full issue/PR bodies, release notes, README,
  contributor lists. Rate-limited (5k req/hr authenticated) → cache aggressively.

## 6. Data flow (detailed)

1. **Ingest** — Airflow DAG pulls each new GH Archive hour; filters to a watchlist of repos
   (start small: ~10–50 repos). Idempotent (re-runnable without dupes).
2. **Land structured** — events normalized into Postgres: `events`, plus derived
   `repo_metrics_hourly` (counts, velocities, merge times).
3. **Enrich + embed** — for issues/PRs in watched repos, fetch text via GitHub API, chunk,
   embed, store in `pgvector` with metadata (repo, type, number, timestamp, url).
4. **Detect** — scheduled job computes rolling baselines per metric and flags statistically
   unusual deviations (z-score / robust stats to start). Emits an `anomaly` row.
5. **Investigate** — the agent is triggered per anomaly; runs its tool loop (§7).
6. **Explain** — agent emits a `report` with narrative, confidence, and citation list.
7. **Serve** — API exposes reports + an on-demand "investigate this repo" endpoint; demo UI
   renders narrative + clickable citations + the agent's trace.

## 7. The investigator agent (centerpiece)

- **Framework:** LangGraph (explicit state machine → debuggable, traceable).
- **Tools available to the agent:**
  - `run_sql(query)` — parameterized, read-only, against the metrics/events tables.
  - `semantic_search(query, repo, k)` — hybrid retrieval over issue/PR text.
  - `get_release_notes(repo, since)` — structured release lookup.
- **Loop:** plan → pick tool → observe → reflect ("is this enough to explain it?") →
  re-query or conclude. Hard cap on steps to bound cost.
- **Routing decision** is the key signal: counting/aggregation → SQL; "why/sentiment/themes"
  → semantic. Document the routing logic explicitly; it's what interviewers probe.
- **Output contract:** `{ summary, root_cause, confidence, citations[] }` — every factual
  claim must map to a citation (a SQL result or a retrieved doc). No uncited claims.

## 8. Evaluation strategy

- **Golden dataset:** 50–100 known GitHub events with hand-written expected explanations
  (e.g. "spike on date D in repo R caused by release V"). This is also a public benchmark.
- **Metrics (RAGAS):** faithfulness/groundedness, context precision, answer relevancy;
  plus retrieval hit-rate and a routing-accuracy check (did it pick the right tool?).
- **CI gate:** GitHub Actions runs eval on a fixed set every PR; fails the build if
  groundedness / retrieval / latency / cost regress past thresholds.

## 9. Observability

- **Langfuse** wired into every agent step: full traces, token cost per call, latency per
  node, and failure spans. This is what anchors the "I owned a production LLM system" story.
- A trace explorer in the demo: each answer links to the SQL run, the retrieved chunks, the
  agent steps, and the token cost.

## 10. Guardrails / safety

- **Prompt-injection tests** (issue/PR text is untrusted input — an issue body could try to
  hijack the agent). Test suite of malicious inputs.
- **Tool allowlist** + read-only SQL (no writes, parameterized, schema-scoped).
- **Cited-outputs-only:** the agent must refuse to assert anything it can't cite.
- Handle bad/missing data gracefully (unknown repo, stale embeddings, empty results).

## 11. Tech stack & rationale

See README table. Principle: **one Postgres** (with pgvector) instead of a separate vector
DB — simpler ops, and a deliberate talking point. Local-first docker-compose; cloud-portable
by design. Off-the-shelf LLM via API (model-agnostic behind an interface).

## 12. Build plan (slices)

> **Working model:** learning-first. Mish writes the core logic; collaborator scaffolds
> boilerplate, explains the *why*, reviews, and quizzes. Each slice ends only when Mish can
> explain it back. Ingestion stays minimal; effort concentrates on the agent + eval/obs.

| # | Slice | Core deliverable | Who writes the core |
|---|---|---|---|
| 0 | Repo + docs | This spec + README + .gitignore | ✅ done |
| 1 | Local stack | `docker-compose.yml`: Postgres+pgvector, Airflow | scaffold + explain |
| 2 | Ingest + prove | Pull a real GH Archive slice → Postgres; eyeball that explainable anomalies exist | pair |
| 3 | Schema + metrics | `events`, `repo_metrics_hourly`; the data model | **Mish** |
| 4 | Detector | Anomaly detection over metrics | **Mish** |
| 5 | Retrieval | Embeddings + hybrid (dense+sparse) search | pair |
| 6 | Agent | LangGraph investigation loop + tool routing | **Mish** (coached) |
| 7 | Eval | Golden set + RAGAS + CI gate | pair |
| 8 | Observability + guardrails | Langfuse traces; injection tests; cited-only | pair |
| 9 | API + demo | FastAPI + Streamlit live demo | **Mish** |
| 10 | Launch | Investigate ~10 marquee repos; first reports | pair |

## 13. Definition of done

- Live public demo: enter a repo → agent returns a cited explanation within seconds.
- A public feed of auto-generated repo-health reports.
- Eval suite green in CI with published scores on the golden benchmark.
- Langfuse traces visible; cost/latency per investigation documented.
- README with architecture, sample investigations, and "run it yourself" instructions.

## 14. Distribution plan

- **Launch cohort:** investigate ~10 high-interest repos — LangChain, vLLM, CrewAI, Supabase,
  DuckDB, Temporal, Airflow, dbt, TiDB, ClickHouse.
- **Content engine:** publish recurring "AI-infra repo health reports" (each a shareable
  artifact). Channels: Show HN (sharp, benefit-led title), active LinkedIn posts, a technical
  blog walkthrough.
- Lead every post with a **finding**, not the architecture.

## 15. Decisions log

- **2026-05-30** — Named `repo-pulse` (was the empty `marketpulse`; finance framing dropped).
- **2026-05-30** — pgvector (single Postgres) over a separate vector DB, for ops simplicity.
- **2026-05-30** — LangGraph over CrewAI for the agent (explicit, debuggable state).
- **2026-05-30** — Skip fine-tuning; hosted LLM behind a model-agnostic interface.

## 16. Open questions

- Exact anomaly-detection method (z-score vs. robust/seasonal) — decide after seeing real data (slice 2).
- Which LLM(s) to default to; cost ceiling per investigation.
- Embedding model choice + chunking strategy for issue/PR text.
- Hosting for the live demo (HF Spaces vs. small VM vs. Fly.io).
- Watchlist size for the live version (cost vs. coverage).
