"""
Run the investigator agent on every golden benchmark case and score the results.

Metrics (all deterministic, no LLM judge required for the base run):
  citation_coverage  — fraction of expected_evidence_urls that appear in citations
                       (N/A if expected_evidence_urls is empty)
  has_citations      — agent produced at least one citation
  budget_ok          — agent used ≤ 6 tool calls
  latency_ok         — agent completed within 90s
  confidence_match   — reported confidence is in the expected set
  tool_calls         — how many tool calls were made
  latency_s          — wall-clock seconds

Optional (--judge flag, costs a few cents per case):
  groundedness       — GPT-4o-mini scores each evidence bullet:
                       "Is this claim supported by a citation? Yes/No"
                       Score = fraction of bullets grounded.

Usage:
    python -m eval.evaluator
    python -m eval.evaluator --judge          # add LLM groundedness scoring
    python -m eval.evaluator --skip-existing  # skip cases that already have reports
"""

import argparse
import json
import time
from datetime import date
from pathlib import Path

from openai import OpenAI

from repopulse.db import get_connection
from repopulse.agent import investigate, InvestigationReport

GOLDEN_CASES = Path(__file__).parent / "golden_cases.json"
RESULTS_PATH = Path("eval/results.json")

# Thresholds for pass/fail in the summary
CITATION_COVERAGE_THRESHOLD = 0.5   # at least 50% of expected URLs cited
LATENCY_THRESHOLD_S         = 90.0
MAX_TOOL_CALLS              = 8


# ── Scoring ────────────────────────────────────────────────────────────────────

def score_citation_coverage(expected_urls: list[str], report: InvestigationReport) -> float | None:
    """
    What fraction of the expected evidence URLs appear in the agent's citations?
    Returns None if no expected URLs are defined (can't score).
    """
    if not expected_urls:
        return None
    cited = {c.source_url for c in report.citations}
    hits  = sum(1 for url in expected_urls if url in cited)
    return round(hits / len(expected_urls), 2)


def score_groundedness(report: InvestigationReport, oai: OpenAI) -> float:
    """
    LLM judge: for each evidence bullet, ask GPT-4o-mini whether it is
    supported by at least one citation in the report.
    Returns fraction of bullets that are grounded (0.0 – 1.0).

    This is the "faithfulness" metric from RAGAS, implemented with a lightweight
    prompt rather than pulling in the full RAGAS library.
    """
    if not report.evidence:
        return 0.0

    citations_text = "\n".join(
        f"- [{c.citation_type}] {c.source_url}: {c.excerpt[:200]}"
        for c in report.citations
    )

    grounded = 0
    for bullet in report.evidence:
        prompt = (
            f"Citations available:\n{citations_text}\n\n"
            f"Claim: {bullet}\n\n"
            "Is this claim directly supported by at least one of the citations above? "
            "Reply with exactly one word: Yes or No."
        )
        resp = oai.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=5,
            temperature=0,
        )
        answer = resp.choices[0].message.content.strip().lower()
        if answer.startswith("yes"):
            grounded += 1

    return round(grounded / len(report.evidence), 2)


def score_case(golden: dict, report: InvestigationReport,
               duration: float, tool_calls: int,
               oai: OpenAI | None = None) -> dict:
    """Compute all metrics for one golden case."""
    coverage = score_citation_coverage(golden.get("expected_evidence_urls", []), report)

    scores = {
        "case_id":          golden["id"],
        "repo":             golden["repo"],
        "anomaly_type":     golden["anomaly_type"],
        "citation_coverage": coverage,                          # None = not scored
        "has_citations":    len(report.citations) > 0,
        "budget_ok":        tool_calls <= MAX_TOOL_CALLS,
        "latency_ok":       duration <= LATENCY_THRESHOLD_S,
        "confidence_match": report.confidence in golden.get("expected_confidences", ["high", "medium", "low"]),
        "confidence":       report.confidence,
        "tool_calls":       tool_calls,
        "latency_s":        round(duration, 1),
        "summary":          report.summary[:120],
    }

    if oai is not None:
        scores["groundedness"] = score_groundedness(report, oai)

    return scores


def format_table(results: list[dict], use_judge: bool) -> str:
    """Format results as a readable table."""
    cols = [
        ("case_id",           30, "Case"),
        ("citation_coverage",  8, "Cit%"),
        ("has_citations",      5, "Cite"),
        ("budget_ok",          4, "Bgt"),
        ("latency_ok",         4, "Lat"),
        ("confidence_match",   4, "Cnf"),
        ("tool_calls",         5, "Tcal"),
        ("latency_s",          6, "Sec"),
    ]
    if use_judge:
        cols.append(("groundedness", 8, "Grnd"))

    header = "  ".join(f"{label:<{w}}" for _, w, label in cols)
    sep    = "  ".join("-" * w for _, w, _ in cols)

    lines = [header, sep]
    for r in results:
        row = []
        for key, width, _ in cols:
            val = r.get(key)
            if isinstance(val, bool):
                cell = "OK " if val else "FAIL"
            elif isinstance(val, float):
                cell = f"{val:.2f}" if val is not None else "N/A"
            elif val is None:
                cell = "N/A"
            else:
                cell = str(val)
            row.append(f"{cell:<{width}}")
        lines.append("  ".join(row))

    # Summary row
    n          = len(results)
    scored_cov = [r["citation_coverage"] for r in results if r["citation_coverage"] is not None]
    avg_cov    = f"{sum(scored_cov)/len(scored_cov):.2f}" if scored_cov else "N/A"
    budget_ok  = sum(1 for r in results if r["budget_ok"])
    latency_ok = sum(1 for r in results if r["latency_ok"])
    conf_ok    = sum(1 for r in results if r["confidence_match"])
    avg_lat    = f"{sum(r['latency_s'] for r in results) / n:.1f}s"

    lines.append("")
    lines.append(f"Summary: {n} cases | avg citation coverage {avg_cov} | "
                 f"budget {budget_ok}/{n} | latency {latency_ok}/{n} | "
                 f"confidence match {conf_ok}/{n} | avg latency {avg_lat}")

    if use_judge and all("groundedness" in r for r in results):
        avg_grnd = sum(r["groundedness"] for r in results) / n
        lines.append(f"         avg groundedness {avg_grnd:.2f}")

    return "\n".join(lines)


# ── Anomaly lookup ─────────────────────────────────────────────────────────────

def get_anomaly_id(conn, case: dict) -> int | None:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id FROM anomalies
            WHERE repo_name = %s
              AND anomaly_type = %s
              AND window_start = %s
              AND window_end = %s
            """,
            (case["repo"], case["anomaly_type"],
             case["window_start"], case["window_end"]),
        )
        row = cur.fetchone()
    return row[0] if row else None


def get_latest_report(conn, anomaly_id: int) -> dict | None:
    """Return the most recent report for this anomaly, if any."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT tool_calls_used, duration_seconds, raw_output
            FROM investigation_reports
            WHERE anomaly_id = %s
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (anomaly_id,),
        )
        return cur.fetchone()


# ── CLI ────────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate the investigator agent on golden cases")
    parser.add_argument("--judge",         action="store_true",
                        help="Add LLM-based groundedness scoring (costs a few cents)")
    parser.add_argument("--skip-existing", action="store_true",
                        help="Re-use reports already stored in the DB (skip re-running the agent)")
    parser.add_argument("--case",          help="Run only one case by id (for debugging)")
    args = parser.parse_args()

    cases = json.loads(GOLDEN_CASES.read_text())
    if args.case:
        cases = [c for c in cases if c["id"] == args.case]
        if not cases:
            print(f"No case found with id={args.case}")
            return

    conn = get_connection()
    oai  = OpenAI() if args.judge else None
    results = []

    for case in cases:
        print(f"\n── {case['id']}")

        anomaly_id = get_anomaly_id(conn, case)
        if anomaly_id is None:
            print(f"  SKIP: anomaly not found — run `python -m eval.prepare` first")
            continue

        # Optionally re-use existing report
        existing = get_latest_report(conn, anomaly_id) if args.skip_existing else None

        if existing:
            print(f"  using existing report (tool_calls={existing[0]}, {existing[1]:.1f}s)")
            tool_calls = existing[0]
            duration   = existing[1]
            # psycopg3 auto-deserializes JSONB → already a dict
            raw    = existing[2] if isinstance(existing[2], dict) else json.loads(existing[2])
            report = InvestigationReport(**raw)
        else:
            print(f"  running agent on anomaly_id={anomaly_id} ...")
            t0 = time.time()
            try:
                report = investigate(anomaly_id)
            except Exception as e:
                duration = time.time() - t0
                print(f"  FAILED ({e.__class__.__name__}: {e})")
                results.append({
                    "case_id":           case["id"],
                    "repo":              case["repo"],
                    "anomaly_type":      case["anomaly_type"],
                    "citation_coverage": None,
                    "has_citations":     False,
                    "budget_ok":         False,
                    "latency_ok":        duration <= LATENCY_THRESHOLD_S,
                    "confidence_match":  False,
                    "confidence":        "error",
                    "tool_calls":        0,
                    "latency_s":         round(duration, 1),
                    "summary":           f"ERROR: {e}",
                })
                continue
            duration   = time.time() - t0
            # Fetch the tool_calls count from the freshly stored report
            stored = get_latest_report(conn, anomaly_id)
            tool_calls = stored[0] if stored else 0

        scores = score_case(case, report, duration, tool_calls, oai)
        results.append(scores)

        # Per-case quick summary
        cov = f"{scores['citation_coverage']:.0%}" if scores["citation_coverage"] is not None else "N/A"
        print(f"  citation_coverage={cov}  confidence={scores['confidence']}  "
              f"tool_calls={scores['tool_calls']}  latency={scores['latency_s']}s")

    conn.close()

    print("\n" + "=" * 80)
    print("EVAL RESULTS")
    print("=" * 80)
    print(format_table(results, args.judge))

    # Persist results
    RESULTS_PATH.parent.mkdir(exist_ok=True)
    RESULTS_PATH.write_text(json.dumps(results, indent=2, default=str))
    print(f"\nResults saved to {RESULTS_PATH}")


if __name__ == "__main__":
    main()
