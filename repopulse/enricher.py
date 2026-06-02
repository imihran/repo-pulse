"""
Fetch PR/issue text from the GitHub API and store in github_artifacts.

For each PR number found in the events table for the given window, this script:
  1. Fetches the PR title, body, labels, and state from GitHub API
  2. Fetches the discussion comments (the text thread, not inline review comments)
  3. Combines everything into a single text blob stored in github_artifacts.body

Why we need the GitHub API (not just the events table):
  GH Archive gives us event counts but the PR body in the payload is sometimes
  truncated. More importantly, comments — which contain the "why" behind slow
  PRs ("waiting for benchmark results", "blocked on X") — are not in GH Archive
  at all. Comments are the primary evidence for the investigator agent.

Rate limits: 5000 req/hr authenticated. We fetch at most --limit PRs and cap
comment pages at 3 (90 comments max) per PR to stay well within budget.

Usage:
    python -m repopulse.enricher --repo langchain-ai/langchain \\
        --start 2025-04-24 --end 2025-04-30 --limit 30
"""

import argparse
import json
import os
import time
from datetime import date

import requests
from pathlib import Path

from dotenv import load_dotenv

from repopulse.db import get_connection

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "")
GH_BASE      = "https://api.github.com"
GH_HEADERS   = {
    "Authorization": f"Bearer {GITHUB_TOKEN}",
    "Accept":        "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
}

MAX_COMMENT_PAGES = 3   # cap at 90 comments per artifact to control API usage


# ── GitHub API helpers ─────────────────────────────────────────────────────────

def gh_get(url: str, params: dict = None) -> dict | list | None:
    """
    GET from GitHub API with basic error handling.
    Returns None on 404, raises on other errors.
    Sleeps briefly on rate-limit (429 / 403 with reset header).
    """
    resp = requests.get(url, headers=GH_HEADERS, params=params, timeout=30)

    if resp.status_code == 404:
        return None

    if resp.status_code in (403, 429):
        # Rate limit hit — back off until the reset time
        reset = int(resp.headers.get("X-RateLimit-Reset", time.time() + 60))
        wait  = max(reset - time.time(), 1)
        print(f"\n  rate limited — sleeping {wait:.0f}s")
        time.sleep(wait)
        return gh_get(url, params)  # retry once after backoff

    resp.raise_for_status()
    return resp.json()


def fetch_pr(owner: str, repo: str, number: int) -> dict | None:
    return gh_get(f"{GH_BASE}/repos/{owner}/{repo}/pulls/{number}")


def fetch_comments(owner: str, repo: str, number: int) -> list[dict]:
    """
    Fetch issue-thread comments for a PR (the narrative discussion).
    Capped at MAX_COMMENT_PAGES pages (30 comments/page).
    Note: inline code-review comments are a separate endpoint — skipped for MVP.
    """
    comments = []
    for page in range(1, MAX_COMMENT_PAGES + 1):
        batch = gh_get(
            f"{GH_BASE}/repos/{owner}/{repo}/issues/{number}/comments",
            params={"per_page": 30, "page": page},
        )
        if not batch:
            break
        comments.extend(batch)
        if len(batch) < 30:
            break   # reached the last page
        time.sleep(0.3)
    return comments


# ── Text assembly ──────────────────────────────────────────────────────────────

def build_text(pr: dict, comments: list[dict]) -> str:
    """
    Combine PR title, body, and comments into one text blob.
    This is what gets chunked and embedded in embedder.py.
    Structured so the embedding captures who said what and in what order.
    """
    parts = [
        f"PR #{pr['number']}: {pr['title']}",
        "",
        pr.get("body") or "(no description)",
    ]

    if comments:
        parts.append("\n---\nDiscussion comments:")
        for c in comments:
            author = c.get("user", {}).get("login", "unknown")
            body   = (c.get("body") or "").strip()
            if body:
                parts.append(f"\n@{author}: {body}")

    return "\n".join(parts)


# ── Database ───────────────────────────────────────────────────────────────────

def get_pr_numbers(conn, repo_name: str, start: date, end: date, limit: int) -> list[int]:
    """
    Get distinct PR numbers from the events table for the given repo and window.
    We pull all PullRequestEvents (not just merges) so we capture PRs that were
    opened, commented on, or reviewed during the anomaly window.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT DISTINCT (payload->'pull_request'->>'number')::int AS pr_number
            FROM events
            WHERE repo_name = %s
              AND type = 'PullRequestEvent'
              AND created_at::date BETWEEN %s AND %s
              AND (payload->'pull_request'->>'number') IS NOT NULL
            ORDER BY pr_number DESC
            LIMIT %s
            """,
            (repo_name, start, end, limit),
        )
        return [row[0] for row in cur.fetchall() if row[0] is not None]


def fetch_merged_prs(owner: str, repo: str, start: date, end: date, limit: int) -> list[int]:
    """
    Use GitHub search API to find PRs merged in the given date range.

    Complements get_pr_numbers() for PRs opened before our events window — e.g.
    a PR open for 373 days that only appears as a close event in the events table
    would be found by get_pr_numbers(), but this call also works when the events
    table is narrower than the PR's lifetime.

    Search API rate limit: 30 req/min (vs 5000/hr for REST). Use sparingly —
    one call per anomaly window is fine.
    """
    data = gh_get(
        f"{GH_BASE}/search/issues",
        params={
            "q": f"repo:{owner}/{repo} type:pr merged:{start}..{end}",
            "sort": "updated",
            "per_page": min(limit, 100),
        },
    )
    if not data:
        return []
    return [item["number"] for item in data.get("items", [])]


def upsert_artifact(conn, repo_name: str, pr: dict, full_text: str) -> int:
    """Store or refresh a PR artifact. Returns the row id."""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO github_artifacts
                (repo_name, type, number, title, body, state,
                 author_login, created_at, closed_at, labels, url)
            VALUES (%s, 'pull_request', %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s)
            ON CONFLICT (repo_name, type, number) DO UPDATE SET
                body       = EXCLUDED.body,
                state      = EXCLUDED.state,
                closed_at  = EXCLUDED.closed_at,
                fetched_at = now()
            RETURNING id
            """,
            (
                repo_name,
                pr["number"],
                pr["title"],
                full_text,
                pr["state"],
                pr.get("user", {}).get("login"),
                pr["created_at"],
                pr.get("closed_at"),
                json.dumps([lb["name"] for lb in pr.get("labels", [])]),
                pr["html_url"],
            ),
        )
        artifact_id = cur.fetchone()[0]
    conn.commit()
    return artifact_id


# ── CLI ────────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Fetch PR text from GitHub API into github_artifacts")
    parser.add_argument("--repo",  required=True, help="owner/repo, e.g. langchain-ai/langchain")
    parser.add_argument("--start", required=True, metavar="YYYY-MM-DD")
    parser.add_argument("--end",   required=True, metavar="YYYY-MM-DD")
    parser.add_argument("--limit", type=int, default=30,
                        help="Max PRs to fetch (default 30, respects the enrichment budget)")
    args = parser.parse_args()

    if not GITHUB_TOKEN:
        print("Error: GITHUB_TOKEN is not set in .env")
        return

    owner, repo = args.repo.split("/")
    start       = date.fromisoformat(args.start)
    end         = date.fromisoformat(args.end)

    conn = get_connection()
    from_events = get_pr_numbers(conn, args.repo, start, end, args.limit)
    from_search = fetch_merged_prs(owner, repo, start, end, args.limit)
    pr_numbers  = sorted(set(from_events) | set(from_search), reverse=True)[:args.limit]
    print(f"Found {len(pr_numbers)} PRs for {args.repo} ({start} → {end})"
          f"  [events={len(from_events)}, search={len(from_search)}]")

    for number in pr_numbers:
        print(f"  PR #{number} ... ", end="", flush=True)

        pr = fetch_pr(owner, repo, number)
        if not pr:
            print("not found (deleted or moved)")
            continue

        comments   = fetch_comments(owner, repo, number)
        full_text  = build_text(pr, comments)
        artifact_id = upsert_artifact(conn, args.repo, pr, full_text)

        print(f"stored (id={artifact_id}, {len(comments)} comments, {len(full_text):,} chars)")
        time.sleep(0.2)  # stay well within the 5000 req/hr rate limit

    conn.close()
    print(f"\nDone: {len(pr_numbers)} PRs enriched.")


if __name__ == "__main__":
    main()
