"""
Slice 6: The LangGraph Investigator Agent

Graph topology (ReAct loop):
    START → agent ⇄ tools  →  (budget exhausted or done) → conclude → END

The agent has three tools and a strict routing rule:
    run_sql          → counting, aggregation, time-series  (structured questions)
    semantic_search  → themes, sentiment, developer intent (unstructured text)
    get_release_notes → check whether a release coincides with the anomaly

Routing rule (what interviewers ask about):
    "How many / trend / average / compare dates?" → run_sql
    "Why / what did people say / what changed?"  → semantic_search
    "Did a release happen around this time?"     → get_release_notes

Budget: ≤ 6 tool calls, 90s wall-clock timeout.
Output: strict Pydantic-validated InvestigationReport — no uncited claims.

Usage:
    python -m repopulse.agent --anomaly-id 1
    python -m repopulse.agent --anomaly-id 1 --model gpt-4o
"""

import argparse
import json
import re
import time
from datetime import date
from typing import Annotated, Literal

from dotenv import load_dotenv
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.tools import tool
from langchain_openai import ChatOpenAI
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode
from openai import OpenAI
from pydantic import BaseModel, Field
from typing_extensions import TypedDict

from repopulse.db import get_connection

load_dotenv()

LLM_MODEL      = "gpt-4o-mini"   # swap to gpt-4o for higher quality; both fit the $0.25 budget
MAX_TOOL_CALLS = 6
TIMEOUT_SECS   = 90


# ── Output contract ────────────────────────────────────────────────────────────
# Every field is required. The agent cannot produce a report unless it can fill
# all of them — this enforces the "cited-outputs-only" guardrail at the type level.

class Citation(BaseModel):
    citation_type: Literal["sql_result", "artifact_chunk", "release_note"]
    source_url:    str   = Field(description="GitHub URL or 'SQL: <description>'")
    excerpt:       str   = Field(description="The specific value or passage cited")

class InvestigationReport(BaseModel):
    summary:     str = Field(description="2-3 sentence plain-English summary of the finding")
    root_cause:  str = Field(description="The most likely explanation for the anomaly")
    confidence:  Literal["high", "medium", "low"]
    evidence:    list[str] = Field(description="Bullet-point list of supporting facts")
    citations:   list[Citation]
    limitations: str = Field(description="What the agent could not determine or verify")


# ── Agent state ────────────────────────────────────────────────────────────────
# TypedDict fields become the graph's state. `add_messages` is a LangGraph
# reducer that appends new messages rather than overwriting the list —
# this is how the conversation history accumulates through the loop.

class AgentState(TypedDict):
    messages:        Annotated[list, add_messages]
    tool_calls_used: int
    start_time:      float


# ── Tools ──────────────────────────────────────────────────────────────────────
# Each tool is a plain Python function decorated with @tool.
# LangGraph reads the function signature + docstring to build the JSON schema
# that gets sent to the LLM as a "function" the model can call.

@tool
def run_sql(query: str) -> str:
    """
    Execute a read-only SQL SELECT query against the RepoPulse database.
    Use this for: counts, trends, averages, comparisons across dates.
    Returns results as a formatted table (max 50 rows).

    Routing rule: use this when the question is about NUMBERS or TRENDS.
    Use semantic_search when the question is about WHY or WHAT PEOPLE SAID.

    EXACT TABLE SCHEMAS (use these column names — no others exist):

    repo_metrics_daily:
      repo_name TEXT, date DATE,
      star_count INT, fork_count INT,
      issue_opened INT, issue_closed INT,
      pr_opened INT, pr_merged INT,
      pr_median_merge_hours FLOAT,   -- NOTE: median, not average
      commit_count INT

    events:
      id TEXT, type TEXT, repo_name TEXT, actor_login TEXT,
      created_at TIMESTAMPTZ, payload JSONB
      -- useful payload paths for PullRequestEvent:
      --   payload->>'action'                          ('opened','closed')
      --   payload->'pull_request'->>'number'
      --   payload->'pull_request'->>'title'
      --   payload->'pull_request'->>'merged'          ('true'/'false')
      --   payload->'pull_request'->>'created_at'
      --   payload->'pull_request'->>'merged_at'

    anomalies:
      id INT, repo_name TEXT, anomaly_type TEXT,
      window_start DATE, window_end DATE,
      metric_name TEXT, baseline_value FLOAT,
      observed_value FLOAT, z_score FLOAT, status TEXT

    github_artifacts:
      id INT, repo_name TEXT, type TEXT, number INT,
      title TEXT, body TEXT, state TEXT, author_login TEXT,
      created_at TIMESTAMPTZ, closed_at TIMESTAMPTZ,
      labels JSONB, url TEXT

    repos: id INT, name TEXT
    """
    # Safety: only allow SELECT / WITH ... SELECT (no writes, no DDL)
    normalized = query.strip().lstrip("-– \n").upper()
    if not re.match(r"^(SELECT|WITH)\b", normalized):
        return "Error: only SELECT queries are permitted."

    conn = get_connection()
    try:
        with conn.cursor() as cur:
            # Enforce a per-statement timeout — prevents a bad query from
            # hanging the agent for the full 90s budget.
            cur.execute("SET statement_timeout = '10s'")
            cur.execute(query)
            rows    = cur.fetchmany(50)
            cols    = [d[0] for d in cur.description]
            if not rows:
                return "Query returned no rows."
            # Format as a simple markdown table for the LLM to read
            header = " | ".join(cols)
            sep    = " | ".join(["---"] * len(cols))
            body   = "\n".join(" | ".join(str(v) for v in row) for row in rows)
            return f"{header}\n{sep}\n{body}"
    except Exception as e:
        return f"SQL error: {e}"
    finally:
        conn.close()


@tool
def semantic_search(query: str, repo: str, k: int = 5) -> str:
    """
    Search artifact chunks (PR/issue text) by semantic similarity.
    Use this for: understanding WHY something happened — themes, developer
    sentiment, what contributors were discussing during the anomaly window.

    Routing rule: use this when the question is about WHY or WHAT PEOPLE SAID.
    Use run_sql when the question is about NUMBERS or TRENDS.

    Args:
        query: natural-language question or topic to search for
        repo:  'owner/repo', e.g. 'langchain-ai/langchain'
        k:     number of chunks to return (default 5, max 10)
    """
    k = min(k, 10)  # cap to stay within the 20-chunk budget

    oai_client = OpenAI()
    embedding  = oai_client.embeddings.create(
        input=[query], model="text-embedding-3-small", dimensions=1536
    ).data[0].embedding

    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT artifact_url,
                       LEFT(text, 400)                            AS excerpt,
                       1 - (embedding <=> %s::vector)             AS similarity
                FROM artifact_chunks
                WHERE repo_name = %s
                ORDER BY embedding <=> %s::vector
                LIMIT %s
                """,
                (str(embedding), repo, str(embedding), k),
            )
            rows = cur.fetchall()
        if not rows:
            return f"No artifact chunks found for {repo}. Run the enricher first."
        results = []
        for url, excerpt, sim in rows:
            results.append(f"[sim={sim:.3f}] {url}\n{excerpt}")
        return "\n\n---\n\n".join(results)
    finally:
        conn.close()


@tool
def get_release_notes(repo: str, since: str) -> str:
    """
    Look up release events for a repo since a given date.
    Use this to check whether a major release or breaking change
    coincides with the anomaly window.

    Args:
        repo:  'owner/repo', e.g. 'langchain-ai/langchain'
        since: 'YYYY-MM-DD'
    """
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT created_at::date                           AS release_date,
                       payload->>'ref'                            AS tag,
                       payload->'release'->>'name'               AS release_name,
                       LEFT(payload->'release'->>'body', 300)    AS notes
                FROM events
                WHERE repo_name = %s
                  AND type = 'ReleaseEvent'
                  AND created_at::date >= %s
                ORDER BY created_at DESC
                LIMIT 10
                """,
                (repo, since),
            )
            rows = cur.fetchall()
        if not rows:
            return f"No release events found for {repo} since {since}."
        lines = [f"{d} — {tag} — {name}\n{notes}" for d, tag, name, notes in rows]
        return "\n\n".join(lines)
    finally:
        conn.close()


# ── Graph nodes ────────────────────────────────────────────────────────────────

TOOLS      = [run_sql, semantic_search, get_release_notes]
TOOL_NODE  = ToolNode(TOOLS)

def build_llm() -> ChatOpenAI:
    return ChatOpenAI(model=LLM_MODEL, temperature=0).bind_tools(TOOLS)


def agent_node(state: AgentState) -> dict:
    """
    Call the LLM with the current message history and available tools.
    The LLM either calls a tool (continues the loop) or responds with
    a plain message (signals it's done gathering evidence).
    """
    llm      = build_llm()
    response = llm.invoke(state["messages"])
    return {"messages": [response]}


def custom_tool_node(state: AgentState) -> dict:
    """
    Execute all tool calls in the latest AIMessage and increment the counter.
    We wrap ToolNode so we can track how many calls have been made.
    """
    last_msg = state["messages"][-1]
    n_calls  = len(getattr(last_msg, "tool_calls", []))
    result   = TOOL_NODE.invoke(state)
    return {
        **result,
        "tool_calls_used": state["tool_calls_used"] + n_calls,
    }


def should_continue(state: AgentState) -> str:
    """
    Routing function after the agent node.
    Returns 'tools' to continue the loop, 'end' to conclude.

    Three reasons to stop:
      1. Agent produced no tool call (it decided it has enough evidence)
      2. Budget exhausted (≥ MAX_TOOL_CALLS tool calls used)
      3. Timeout elapsed (> TIMEOUT_SECS seconds since start)
    """
    last_msg = state["messages"][-1]
    has_tool_call = bool(getattr(last_msg, "tool_calls", []))

    if not has_tool_call:
        return "end"

    if state["tool_calls_used"] >= MAX_TOOL_CALLS:
        return "end"   # budget exhausted — conclude with what we have

    elapsed = time.time() - state["start_time"]
    if elapsed > TIMEOUT_SECS:
        return "end"   # timeout — same

    return "tools"


# ── Graph assembly ─────────────────────────────────────────────────────────────
# Explicit state machine — every transition is visible and debuggable.
# This is the property that distinguishes LangGraph from a simple while loop.

def build_graph():
    workflow = StateGraph(AgentState)
    workflow.add_node("agent", agent_node)
    workflow.add_node("tools", custom_tool_node)
    workflow.add_edge(START, "agent")
    workflow.add_conditional_edges(
        "agent",
        should_continue,
        {"tools": "tools", "end": END},
    )
    workflow.add_edge("tools", "agent")
    return workflow.compile()


# ── Structured report generation ───────────────────────────────────────────────
# After the investigation loop, we do one final LLM call with structured output.
# This is separate from the loop because with_structured_output disables tool
# calling — mixing them in the same call would require a more complex prompt.

def sanitize_messages(messages: list) -> list:
    """
    If the budget cuts off the loop mid-turn, the last AIMessage may have
    tool_calls with no corresponding ToolMessages. OpenAI rejects that.
    Replace any such dangling AIMessage with a plain message so the
    final structured call succeeds.
    """
    from langchain_core.messages import AIMessage, ToolMessage

    msgs      = list(messages)
    last      = msgs[-1] if msgs else None
    if last is None or not getattr(last, "tool_calls", []):
        return msgs

    resolved  = {m.tool_call_id for m in msgs if isinstance(m, ToolMessage)}
    pending   = [tc for tc in last.tool_calls if tc["id"] not in resolved]
    if pending:
        msgs[-1] = AIMessage(
            content="Budget exhausted. Proceeding to report with evidence gathered so far."
        )
    return msgs


def generate_report(messages: list, anomaly_context: str) -> InvestigationReport:
    """
    Take the full conversation (including all tool outputs) and ask the LLM
    to produce a validated InvestigationReport in one structured call.
    """
    messages   = sanitize_messages(messages)
    llm        = ChatOpenAI(model=LLM_MODEL, temperature=0)
    structured = llm.with_structured_output(InvestigationReport)

    synthesis_prompt = (
        "You have just completed an investigation of the following anomaly:\n\n"
        f"{anomaly_context}\n\n"
        "Based on the evidence you gathered above, produce a final investigation report.\n"
        "Every claim in 'evidence' must have a corresponding citation.\n"
        "If you could not find strong evidence, reflect that in confidence=low "
        "and explain the gap in limitations."
    )

    report_messages = messages + [HumanMessage(content=synthesis_prompt)]
    return structured.invoke(report_messages)


# ── Database helpers ───────────────────────────────────────────────────────────

def load_anomaly(anomaly_id: int) -> dict:
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, repo_name, anomaly_type, window_start, window_end,
                       metric_name, baseline_value, observed_value, z_score
                FROM anomalies WHERE id = %s
                """,
                (anomaly_id,),
            )
            row  = cur.fetchone()
            cols = [d[0] for d in cur.description]
            return dict(zip(cols, row)) if row else None
    finally:
        conn.close()


def store_report(anomaly_id: int, report: InvestigationReport,
                 duration: float, tool_calls: int) -> int:
    """Write the report and its citations to the database. Returns report id."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO investigation_reports
                    (anomaly_id, summary, root_cause, confidence, limitations,
                     tool_calls_used, duration_seconds, raw_output)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb)
                RETURNING id
                """,
                (
                    anomaly_id,
                    report.summary,
                    report.root_cause,
                    report.confidence,
                    report.limitations,
                    tool_calls,
                    round(duration, 2),
                    report.model_dump_json(),
                ),
            )
            report_id = cur.fetchone()[0]

            # Store citations
            for c in report.citations:
                cur.execute(
                    """
                    INSERT INTO report_citations (report_id, citation_type, source_url, excerpt)
                    VALUES (%s, %s, %s, %s)
                    """,
                    (report_id, c.citation_type, c.source_url, c.excerpt),
                )

        conn.commit()
        return report_id
    finally:
        conn.close()


# ── Entry point ────────────────────────────────────────────────────────────────

def investigate(anomaly_id: int, model: str = LLM_MODEL) -> InvestigationReport:
    """
    Run the full investigation loop for an anomaly and return a validated report.
    Also persists the report to investigation_reports + report_citations.
    """
    global LLM_MODEL
    LLM_MODEL = model

    anomaly = load_anomaly(anomaly_id)
    if not anomaly:
        raise ValueError(f"No anomaly with id={anomaly_id}")

    # Format the anomaly as a clear, factual prompt
    anomaly_context = (
        f"Repository:  {anomaly['repo_name']}\n"
        f"Anomaly:     {anomaly['anomaly_type']}\n"
        f"Window:      {anomaly['window_start']} to {anomaly['window_end']}\n"
        f"Metric:      {anomaly['metric_name']}\n"
        f"Baseline:    {anomaly['baseline_value']:.1f} (prior 28-day median)\n"
        f"Observed:    {anomaly['observed_value']:.1f} (last 7-day median)\n"
        f"Z-score:     {anomaly['z_score']:.2f}"
    )

    system_prompt = f"""You are an autonomous investigator analyzing GitHub repository health anomalies.

ANOMALY TO INVESTIGATE:
{anomaly_context}

TOOLS AND ROUTING RULE:
- run_sql        → use for NUMBERS and TRENDS (counts, averages, time series)
- semantic_search → use for WHY and WHAT PEOPLE SAID (themes, discussion, intent)
- get_release_notes → use to check if a release coincides with the anomaly

Routing rule in one sentence: if you need to count or measure, use SQL;
if you need to understand, use semantic search.

INVESTIGATION STRATEGY:
1. Start with SQL to confirm the anomaly in the data and understand its shape
2. Use semantic search to find developer discussions that explain WHY
3. Check release notes if a major version change seems relevant
4. Stop when you have enough evidence for a confident root-cause hypothesis

BUDGET: you have at most {MAX_TOOL_CALLS} tool calls. Plan before you call."""

    graph      = build_graph()
    start_time = time.time()

    initial_state: AgentState = {
        "messages":        [SystemMessage(content=system_prompt),
                            HumanMessage(content="Begin your investigation.")],
        "tool_calls_used": 0,
        "start_time":      start_time,
    }

    final_state = graph.invoke(initial_state)

    duration   = time.time() - start_time
    tool_calls = final_state["tool_calls_used"]

    print(f"\n  Investigation complete: {tool_calls} tool calls, {duration:.1f}s")

    report    = generate_report(final_state["messages"], anomaly_context)
    report_id = store_report(anomaly_id, report, duration, tool_calls)

    print(f"  Report stored: id={report_id}")
    return report


# ── CLI ────────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Run the investigator agent on an anomaly")
    parser.add_argument("--anomaly-id", type=int, required=True,
                        help="Row id from the anomalies table")
    parser.add_argument("--model", default=LLM_MODEL,
                        help=f"OpenAI model to use (default: {LLM_MODEL})")
    args = parser.parse_args()

    report = investigate(args.anomaly_id, model=args.model)

    print("\n" + "=" * 60)
    print("INVESTIGATION REPORT")
    print("=" * 60)
    print(f"\nSummary:\n  {report.summary}")
    print(f"\nRoot cause:\n  {report.root_cause}")
    print(f"\nConfidence: {report.confidence.upper()}")
    print(f"\nEvidence:")
    for e in report.evidence:
        print(f"  • {e}")
    print(f"\nCitations ({len(report.citations)}):")
    for c in report.citations:
        print(f"  [{c.citation_type}] {c.source_url}")
        print(f"    {c.excerpt[:120]}")
    print(f"\nLimitations:\n  {report.limitations}")


if __name__ == "__main__":
    main()
