"""
Export investigation reports from Postgres to static JSON files.

Writes:
  web/data/reports.json          — feed index (all reports, summary fields only)
  web/data/reports/{id}.json     — full report detail per investigation

Run before deploying to Cloudflare Pages:
    python -m repopulse.export

The frontend reads these static files directly — no live backend required.
"""

import json
from datetime import date, datetime
from pathlib import Path

from repopulse.db import get_connection

WEB_DATA_DIR = Path(__file__).resolve().parent.parent / "web" / "data"


def decimal_default(obj):
    """JSON serializer for types that aren't serializable by default."""
    if isinstance(obj, (date, datetime)):
        return obj.isoformat()
    raise TypeError(f"Type {type(obj)} not serializable")


def fetch_reports(conn) -> list[dict]:
    """Fetch all reports joined with their anomaly and citations."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT
                r.id,
                r.created_at,
                r.summary,
                r.root_cause,
                r.confidence,
                r.limitations,
                r.tool_calls_used,
                r.duration_seconds,
                r.raw_output,
                a.repo_name,
                a.anomaly_type,
                a.window_start,
                a.window_end,
                a.metric_name,
                a.baseline_value,
                a.observed_value,
                a.z_score
            FROM investigation_reports r
            JOIN anomalies a ON a.id = r.anomaly_id
            ORDER BY r.created_at DESC
        """)
        cols = [d[0] for d in cur.description]
        rows = [dict(zip(cols, row)) for row in cur.fetchall()]

    # Attach citations to each report
    for row in rows:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT citation_type, source_url, excerpt
                FROM report_citations
                WHERE report_id = %s
            """, (row["id"],))
            row["citations"] = [
                {"type": c[0], "url": c[1], "excerpt": c[2]}
                for c in cur.fetchall()
            ]

        # Parse evidence out of raw_output if not already a list
        raw = row.get("raw_output") or {}
        if isinstance(raw, str):
            raw = json.loads(raw)
        row["evidence"] = raw.get("evidence", [])

    return rows


def write_feed(reports: list[dict]) -> None:
    """Write the index feed — summary fields only, no full evidence/citations."""
    feed = []
    for r in reports:
        feed.append({
            "id":            r["id"],
            "repo":          r["repo_name"],
            "anomaly_type":  r["anomaly_type"],
            "window_start":  r["window_start"],
            "window_end":    r["window_end"],
            "metric_name":   r["metric_name"],
            "baseline":      round(float(r["baseline_value"] or 0), 1),
            "observed":      round(float(r["observed_value"] or 0), 1),
            "z_score":       round(float(r["z_score"] or 0), 2),
            "confidence":    r["confidence"],
            "summary":       r["summary"],
            "root_cause":    r["root_cause"],
            "citations_count": len(r["citations"]),
            "tool_calls":    r["tool_calls_used"],
            "duration_s":    round(float(r["duration_seconds"] or 0), 1),
            "created_at":    r["created_at"],
        })

    path = WEB_DATA_DIR / "reports.json"
    path.write_text(json.dumps(feed, indent=2, default=decimal_default))
    print(f"  wrote {path} ({len(feed)} reports)")


def write_detail(report: dict) -> None:
    """Write the full report detail file."""
    detail = {
        "id":           report["id"],
        "repo":         report["repo_name"],
        "anomaly_type": report["anomaly_type"],
        "window_start": report["window_start"],
        "window_end":   report["window_end"],
        "metric_name":  report["metric_name"],
        "baseline":     round(float(report["baseline_value"] or 0), 1),
        "observed":     round(float(report["observed_value"] or 0), 1),
        "z_score":      round(float(report["z_score"] or 0), 2),
        "summary":      report["summary"],
        "root_cause":   report["root_cause"],
        "confidence":   report["confidence"],
        "evidence":     report["evidence"],
        "citations":    report["citations"],
        "limitations":  report["limitations"],
        "tool_calls":   report["tool_calls_used"],
        "duration_s":   round(float(report["duration_seconds"] or 0), 1),
        "created_at":   report["created_at"],
    }

    path = WEB_DATA_DIR / "reports" / f"{report['id']}.json"
    path.write_text(json.dumps(detail, indent=2, default=decimal_default))
    print(f"  wrote {path}")


def main() -> None:
    WEB_DATA_DIR.mkdir(parents=True, exist_ok=True)
    (WEB_DATA_DIR / "reports").mkdir(exist_ok=True)

    conn    = get_connection()
    reports = fetch_reports(conn)
    conn.close()

    print(f"Exporting {len(reports)} reports...")
    write_feed(reports)
    for r in reports:
        write_detail(r)
    print("Done. Deploy the web/ directory to Cloudflare Pages.")


if __name__ == "__main__":
    main()
