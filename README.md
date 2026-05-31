# RepoPulse

> **Datadog for open-source repo health.**
> An AI agent that watches public GitHub projects, notices when a repo's activity meaningfully changes, and **autonomously investigates and explains *why*** — citing the exact PRs, issues, and releases behind the change.

> ⚠️ Status: **early build.** This README describes the intended system so the flow is clear before code exists. See [`PROJECT.md`](./PROJECT.md) for the full engineering spec and build plan.

---

## What it is (in plain English)

Most GitHub analytics tools answer questions you already know to ask ("how many stars did repo X get?"). RepoPulse is different: it runs continuously, **spots unusual changes on its own**, and then acts like an on-call analyst — it digs through the repo's recent activity and writes a short, cited explanation of what happened.

Example output:

> 🔺 **`vllm-project/vllm` — issue-open rate up 4× this week.**
> Most likely cause: the **v0.x.0** release (3 days ago) introduced a regression in the CUDA backend. 12 of the 41 new issues reference `CUDA out of memory` and link to **PR #XXXX**. Maintainer response time has also risen from ~6h to ~30h.
> *Confidence: high. Evidence: 3 issues, 1 PR, 1 release — see citations.*

## The problem it solves

When an open-source project's behavior shifts — a spike in issues, a slowdown in PR merges, a drop in contributors — *someone* has to manually scroll through dozens of issues and PRs to figure out why. RepoPulse automates that **detect → investigate → explain** loop.

**v1 focus:** engineering teams evaluating whether an open-source **dependency** is getting healthier or riskier — and why. (Maintainers, DevRel, and ecosystem-watchers come later.)

---

## How it works — the flow

The whole system is one pipeline with a smart agent at the end. Follow a single anomaly through it:

```
  ┌──────────────┐   1. INGEST          Pull hourly GitHub events (GH Archive)
  │  GH Archive  │──────────────────▶   + enrich via GitHub API (issue/PR/release text)
  │ + GitHub API │
  └──────────────┘
          │
          ▼
  ┌──────────────┐   2. STORE           Structured events → Postgres tables
  │  Postgres    │                      Unstructured text (issues/PRs) → embeddings
  │  + pgvector  │                      in pgvector (for semantic search)
  └──────────────┘
          │
          ▼
  ┌──────────────┐   3. DETECT          Scheduled job scans metrics (star velocity,
  │  Anomaly     │                      issue-open rate, PR merge time...) and flags
  │  detector    │                      statistically unusual changes — no human asks.
  └──────────────┘
          │  (anomaly fires)
          ▼
  ┌──────────────┐   4. INVESTIGATE     The agent decides, step by step, how to dig in:
  │  Investigator│                        • query Postgres (SQL) for the hard numbers
  │  AGENT       │                        • semantic-search issue/PR text for the "why"
  │ (LangGraph)  │                        • read the relevant releases
  │              │                      It loops, re-queries if evidence is weak.
  └──────────────┘
          │
          ▼
  ┌──────────────┐   5. EXPLAIN         Writes a short, **cited** root-cause narrative
  │  Report +    │                      with a confidence level. Every claim links to
  │  citations   │                      the SQL result or the exact issue/PR it used.
  └──────────────┘
          │
          ▼
  ┌──────────────┐   6. SERVE           Public feed of auto-generated "repo health reports"
  │  API + demo  │                      (precomputed) + an on-demand "investigate this repo".
  └──────────────┘

  Wrapped around all of it:  EVAL (is the explanation correct?) ·
  OBSERVABILITY (traces, cost, latency) · GUARDRAILS (injection-safe, cited-only).
```

### The step that matters most (step 4)

The "magic" is the **agent's decision-making**, not the plumbing. For each investigation it chooses the right tool:

- **Counting questions** ("how many issues, how fast are PRs merging?") → it writes and runs **SQL** against Postgres.
- **"Why" questions** ("what are people complaining about?") → it **semantic-searches** the issue/PR text via embeddings.
- It combines both, and if the evidence is thin it loops back and tries another query before answering.

That autonomous routing + the cited explanation is what separates RepoPulse from existing "ask GitHub a question" tools.

---

## Tech stack

| Layer | Tool | Why |
|---|---|---|
| Ingestion / orchestration | **Python runner first → Airflow later** | Prove the loop with a simple scheduled script; add Airflow for production-grade scheduling once the agent works |
| Storage (structured) | **Postgres** | The numbers: events, metrics, time series |
| Storage (semantic) | **pgvector** | Embeddings for issue/PR text — one DB, not two |
| Retrieval | **Vector (pgvector) first → hybrid later** | Vector-only for the first loop; add dense + BM25/sparse once it's closed |
| Agent | **LangGraph** | Explicit, debuggable investigation state machine |
| Evaluation | **RAGAS + a golden dataset** | Prove the explanations are faithful/grounded |
| Observability | **Langfuse** | Traces, token cost, latency per agent step |
| API / demo | **FastAPI + Streamlit (or Gradio)** | Live, public, "type a repo and watch it work" |

Built **local-first via docker-compose**, designed to be **cloud-portable** (Postgres→a managed warehouse, local Airflow→MWAA, etc.).

## Project structure

Folders appear as we build each phase (kept lean on purpose — no empty scaffolding):

```
repo-pulse/
├── README.md            ← you are here (the overview + flow)
├── PROJECT.md           ← full engineering spec + build plan
├── docker-compose.yml   ← local stack (added in Phase 1)
├── ingestion/           ← GH Archive pull + GitHub API enrichment
├── db/                  ← schema + migrations
├── detector/            ← anomaly detection jobs
├── retrieval/           ← hybrid search (dense + sparse)
├── agent/               ← the LangGraph investigation agent
├── eval/                ← RAGAS golden dataset + CI gate
├── api/                 ← FastAPI service
├── app/                 ← Streamlit/Gradio live demo
└── docs/                ← architecture notes, sample investigations
```

## Build roadmap (high level)

Detailed slice-by-slice plan in [`PROJECT.md`](./PROJECT.md).

**MVP:** 3 repos (vLLM, LangChain, dbt-core) × 3 anomaly types (issue-open spike, PR
merge-time slowdown, star/activity spike) → a few excellent cited reports + a 10-case eval set.

1. **Minimal stack** — Postgres + pgvector locally (Airflow comes later, not first).
2. **Ingest + prove** — pull a real GH Archive slice; confirm explainable anomalies exist.
3. **Data model + detector** — daily metrics; flag anomalies (last 7d vs prior 28d).
4. **Retrieval** — embed issue/PR text (vector-only first; hybrid later).
5. **Investigator agent** — the SQL-vs-semantic routing loop (the centerpiece).
6. **Eval + observability + guardrails** — 10 benchmark cases, traces, injection-safe.
7. **Public demo** — feed of precomputed reports first; on-demand investigation second.
8. **Harden + launch** — add Airflow, expand the benchmark, publish marquee-repo reports.

---

*RepoPulse is a learning-first build: the core logic is written by hand and documented as it's built, so every design decision can be explained, not just shipped.*
