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

**v1 wedge user:** engineering teams evaluating **open-source dependency health** — *"is this
repo getting healthier or riskier, and why?"* (Broader audiences — maintainers, DevRel,
investors — come later. A sharp wedge makes the reports sharper.)

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
- Be **publicly demoable** — a feed of **precomputed** repo-health reports first; an optional
  "run a fresh investigation" path second (which may take longer than the feed).
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
- **GitHub REST/GraphQL API** — enrichment / the "why" evidence. Issue/PR *bodies alone are
  not enough.* Minimum useful evidence per artifact: **issue body + issue comments**, **PR
  title/body + PR comments/reviews**, **release notes**, plus **labels, timestamps, and source
  URLs**. Rate-limited (5k req/hr authenticated) → cache aggressively.
- **Enrichment budget (to respect the 5k req/hr limit):** only enrich the **top N artifacts**
  per anomaly window (e.g. N ≤ 30, ranked by comment/reaction volume); prefer **GraphQL
  batching**; use **ETag/conditional requests + on-disk cache**; cap pages per artifact; on
  rate-limit, back off and proceed with **partial evidence** (recorded in `limitations`).

## 6. Data flow (detailed)

1. **Ingest** — a **CLI/Python script** (not Airflow yet) downloads bounded GH Archive
   hours/days and filters to the **3 MVP repos**. Idempotent (re-runnable without dupes).
   Airflow replaces this scheduler in a later slice.
2. **Land structured** — events normalized into Postgres: `events`, plus derived
   `repo_metrics_daily` (counts, velocities, merge times).
3. **Enrich + embed** — for issues/PRs in watched repos, fetch text via GitHub API, chunk,
   embed, store in `pgvector` with metadata (repo, type, number, timestamp, url).
4. **Detect** — scheduled job computes **daily** metrics and flags anomalies by comparing the
   **last 7 days to the prior 28**, using a robust z-score (MAD) or simple ratio threshold,
   gated by a **minimum event count** to suppress noise. Emits an `anomaly` row.
5. **Investigate** — the agent is triggered per anomaly; runs its tool loop (§7).
6. **Explain** — agent emits a `report` with narrative, confidence, and citation list.
7. **Serve** — API exposes the precomputed report feed + an on-demand "investigate this repo"
   endpoint; demo UI renders narrative + clickable citations + the agent's trace.

**Core tables:** `repos`, `events`, `repo_metrics_daily`, `github_artifacts`,
`artifact_chunks`, `anomalies`, `investigation_reports`, `report_citations`.

## 7. The investigator agent (centerpiece)

- **Framework:** LangGraph (explicit state machine → debuggable, traceable).
- **Tools available to the agent:**
  - `run_sql(query)` — parameterized, read-only, against the metrics/events tables.
  - `semantic_search(query, repo, k)` — hybrid retrieval over issue/PR text.
  - `get_release_notes(repo, since)` — structured release lookup.
- **Loop:** plan → pick tool → observe → reflect ("is this enough to explain it?") →
  re-query or conclude. **Default budget (MVP):** ≤ 6 tool calls, ≤ 20 retrieved chunks,
  ≤ ~8k context tokens, 90s timeout, target **< $0.25 / report**.
- **Routing decision** is the key signal: counting/aggregation → SQL; "why/sentiment/themes"
  → semantic. Document the routing logic explicitly; it's what interviewers probe.
- **Output contract (strict JSON, Pydantic-validated):**
  `{ summary, root_cause, confidence, evidence[], citations[], limitations }` — every factual
  claim must map to a citation (a SQL result or a retrieved doc). No uncited claims.

## 8. Evaluation strategy

- **Golden dataset:** start with **10 hand-picked cases** across the 3 MVP anomaly types;
  expand toward 50–100 only after the agent loop works. Doubles as a public benchmark.
- **A golden case =** `{ repo, anomaly_type, date_window, expected_evidence_urls[],
  accepted_explanation, limitation_notes }`.
- **Metrics:** citation coverage, groundedness/faithfulness, retrieval hit-rate, routing/
  tool-choice accuracy, and latency + cost (RAGAS for the LLM-judged metrics).
- **CI gate:** added **after** the eval script runs locally — GitHub Actions then runs eval on
  the fixed set every PR and fails the build if groundedness / retrieval / latency / cost regress.

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

**Keep the first implementation boring:** plain Python with **CLI scripts first, FastAPI only
when needed**; psycopg/SQLAlchemy without over-abstraction early; **Pydantic** for agent/report
contracts. No large empty scaffold — add folders only when they hold working code.

## 12. MVP scope & build plan

**MVP = a narrow, impressive vertical slice** (per external audit): prove the
detect→investigate→explain loop *before* building orchestration.

- **Repos (3):** `vllm-project/vllm`, `langchain-ai/langchain`, `dbt-labs/dbt-core`
- **Anomaly types (3):** issue-open spike · PR merge-time slowdown · star/activity spike
- **First milestone:** a few *excellent* cited reports + a 10-case eval benchmark.

> **Working model:** learning-first. Mish writes the core logic; collaborator scaffolds
> boilerplate, explains the *why*, reviews, and quizzes. Each slice ends only when Mish can
> explain it back. Ingestion stays minimal; effort concentrates on the agent + eval/obs.

| # | Slice | Core deliverable | Who writes core |
|---|---|---|---|
| 0 | Repo + docs | This spec + README + .gitignore | ✅ done |
| 1 | Minimal local stack | docker-compose: **Postgres + pgvector only** (no Airflow yet) | scaffold + explain |
| 2 | Ingest + prove | Bounded GH Archive slice for the 3 repos → idempotent `events`; eyeball that explainable anomalies exist | pair |
| 3 | Data model + metrics | Tables: `repos`, `events`, `repo_metrics_daily`, `github_artifacts`, `artifact_chunks`, `anomalies`, `investigation_reports`, `report_citations` | **Mish** |
| 4 | Detector | Daily metrics; last-7d vs prior-28d; robust z-score/MAD or ratio + min event count | **Mish** |
| 5 | Retrieval | Embed artifact chunks → pgvector; **vector-only OK for first loop** (BM25/hybrid later, documented as a limitation) | pair |
| 6 | Investigator agent | LangGraph loop; tools `run_sql`/`search_artifacts`/`get_release_notes`; step + token caps; strict-JSON output | **Mish** (coached) |
| 7 | Eval | 10 benchmark cases; citation coverage, groundedness, retrieval, routing, latency/cost; CI gate **after** it runs locally | pair |
| 8 | Observability + guardrails | Langfuse traces/cost/latency; prompt-injection tests; read-only SQL; tool allowlist; cited-only validation | pair |
| 9 | Public demo | **Feed of precomputed reports first**; report detail w/ citations + trace; optional on-demand investigation | **Mish** |
| 10 | Harden / Airflow | Add **Airflow** for scheduled ingestion (DE credibility); add hybrid retrieval; expand golden set | pair |
| 11 | Launch | Investigate the marquee repos; publish the first health reports | pair |

**Slice-2 ingest bound (concrete):** download ~**60 days of hourly** GH Archive files,
**stream-decompress line-by-line**, keep only events for the **3 MVP repos** (match by repo
id/name), and idempotently upsert into `events`. Cache raw downloads under `data/` (gitignored)
with a manifest of fetched hours. **Success criteria:** filtered events present for all 3 repos
with visible day-to-day variation; **document the actual GB downloaded** (target: keep it
modest by filtering during decompression, not after).

## 13. Definition of done

- A public **feed of precomputed repo-health reports** (the primary demo), plus an optional
  on-demand "investigate this repo" path (allowed to be slower than the feed).
- Each report: cited root-cause narrative, confidence, an evidence table, links to SQL results
  and source GitHub artifacts, and — once observability is in — the agent trace + cost/latency.
- For the first demo: a **local eval run with published scores** on the (10-case) golden
  benchmark. (The CI eval gate is added later, at launch-hardening — not required for v1.)
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
- **2026-05-30** — (audit) MVP = 3 repos × 3 anomaly types; first eval = 10 cases.
- **2026-05-30** — (audit) Defer Airflow past the first loop — CLI/Python runner first; Airflow added as a later hardening slice.
- **2026-05-30** — (audit) MVP detector = daily metrics, last-7d vs prior-28d, robust z-score/MAD + min event count.
- **2026-05-30** — (audit) v1 wedge = engineering teams evaluating OSS dependency health.
- **2026-05-30** — (audit) Precomputed report feed is the primary demo; on-demand investigation may be slower.

## 16. Open questions

- Tune detector thresholds (z-score cutoff, min event count) against real data in slice 2.
- Which LLM(s) to default to (per-investigation budget defaults set in §7; revisit the ceiling after real runs).
- Embedding model choice + chunking strategy for issue/PR text.
- Whether/when hybrid (BM25) retrieval is worth it vs. vector-only.
- Hosting for the live demo (HF Spaces vs. small VM vs. Fly.io).
