"""
Prepare the golden benchmark cases for evaluation.

For each case in golden_cases.json this script:
  1. Ensures the anomaly row exists in the anomalies table (inserts if missing)
  2. Runs the enricher for that window (fetches PR text from GitHub API)
  3. Runs the embedder for that repo (chunks + embeds into pgvector)

Run once before eval/evaluator.py. Safe to re-run — all operations are idempotent.

Usage:
    python -m eval.prepare
    python -m eval.prepare --dry-run   # show what would be done without doing it
"""

import argparse
import json
from datetime import date
from pathlib import Path

from repopulse.db import get_connection
from repopulse.enricher import (
    GITHUB_TOKEN, fetch_comments, fetch_pr,
    build_text, get_pr_numbers, upsert_artifact,
)
from repopulse.embedder import get_unenriched, embed_batch, store_chunks, chunk_text
from openai import OpenAI

GOLDEN_CASES = Path(__file__).parent / "golden_cases.json"


def ensure_anomaly(conn, case: dict) -> int:
    """
    Return the anomaly_id for this golden case.
    Insert the row if it doesn't exist yet.
    """
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
        if row:
            return row[0]

        # Insert from the golden case metadata
        cur.execute(
            """
            INSERT INTO anomalies
                (repo_name, anomaly_type, window_start, window_end,
                 metric_name, baseline_value, observed_value, z_score)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (
                case["repo"], case["anomaly_type"],
                case["window_start"], case["window_end"],
                case["metric_name"],
                case["baseline_value"], case["observed_value"], case["z_score"],
            ),
        )
        anomaly_id = cur.fetchone()[0]
    conn.commit()
    return anomaly_id


def enrich_window(case: dict, conn, dry_run: bool) -> int:
    """
    Fetch PRs for the anomaly window and store in github_artifacts.
    Returns the number of artifacts enriched.
    """
    if not GITHUB_TOKEN:
        print("    skipping enrichment — GITHUB_TOKEN not set")
        return 0

    owner, repo = case["repo"].split("/")
    start = date.fromisoformat(case["window_start"])
    end   = date.fromisoformat(case["window_end"])
    pr_numbers = get_pr_numbers(conn, case["repo"], start, end, limit=30)

    if not pr_numbers:
        print(f"    no PRs found in events for this window")
        return 0

    if dry_run:
        print(f"    would enrich {len(pr_numbers)} PRs")
        return len(pr_numbers)

    enriched = 0
    for number in pr_numbers:
        pr = fetch_pr(owner, repo, number)
        if not pr:
            continue
        comments  = fetch_comments(owner, repo, number)
        full_text = build_text(pr, comments)
        upsert_artifact(conn, case["repo"], pr, full_text)
        enriched += 1

    return enriched


def embed_repo(repo: str, conn, oai_client: OpenAI, dry_run: bool) -> int:
    """
    Chunk + embed all unenriched artifacts for a repo.
    Returns the number of chunks created.
    """
    artifacts = get_unenriched(conn, repo, limit=200)
    if not artifacts:
        return 0
    if dry_run:
        print(f"    would embed {len(artifacts)} artifacts")
        return len(artifacts)

    total = 0
    for artifact in artifacts:
        text = (artifact.get("body") or "").strip()
        if not text:
            continue
        header = f"[{artifact['type'].upper()} #{artifact['number']}] {artifact['title']}"
        chunks = chunk_text(text, header)
        artifact["repo_name"] = repo
        embeddings = embed_batch(oai_client, chunks)
        store_chunks(conn, artifact, chunks, embeddings)
        total += len(chunks)

    return total


def main() -> None:
    parser = argparse.ArgumentParser(description="Prepare golden benchmark cases")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would be done without making API calls")
    args = parser.parse_args()

    cases = json.loads(GOLDEN_CASES.read_text())
    conn  = get_connection()
    oai   = OpenAI()

    for case in cases:
        print(f"\n── {case['id']}")

        # 1. Ensure anomaly row
        anomaly_id = ensure_anomaly(conn, case)
        print(f"  anomaly_id={anomaly_id}")

        # 2. Enrich the window
        print(f"  enriching {case['window_start']} → {case['window_end']} ...")
        n_enriched = enrich_window(case, conn, args.dry_run)
        print(f"  enriched {n_enriched} artifacts")

        # 3. Embed any newly enriched artifacts for this repo.
        # We call this after every enrich (not once per repo) so new artifacts
        # from later cases in the same repo don't get skipped.
        print(f"  embedding unenriched artifacts for {case['repo']} ...")
        n_chunks = embed_repo(case["repo"], conn, oai, args.dry_run)
        print(f"  embedded {n_chunks} chunks")

    conn.close()
    print("\nPreparation complete. Run: python -m eval.evaluator")


if __name__ == "__main__":
    main()
