"""
Ingest GH Archive events for the 3 MVP repos into Postgres.

GH Archive publishes one .json.gz file per hour:
    https://data.gharchive.org/YYYY-MM-DD-H.json.gz

Each file contains every public GitHub event for that hour — typically
300k–600k JSON lines. We stream-decompress and filter to watched repos
on the fly, so we never hold the full file in memory.

Filtered events are saved as small .jsonl files under data/filtered/,
and a manifest tracks which hours have already been ingested so the
script is safe to re-run (idempotent).

Usage:
    python -m repopulse.ingest --start 2025-03-01 --end 2025-03-07
    python -m repopulse.ingest --start 2025-03-01 --days 7
"""

import argparse
import gzip
import json
import sys
from datetime import date, timedelta
from pathlib import Path

import requests

from repopulse.db import get_connection

# Exact repo names as they appear in the GH Archive 'repo.name' field.
WATCHED_REPOS = {
    "vllm-project/vllm",
    "langchain-ai/langchain",
    "dbt-labs/dbt-core",
}

DATA_DIR = Path("data")
FILTERED_DIR = DATA_DIR / "filtered"
MANIFEST_PATH = DATA_DIR / "manifest.json"


# ── URL helpers ────────────────────────────────────────────────────────────────

def gharchive_url(dt: date, hour: int) -> str:
    # GH Archive hours are 0-indexed: ...-0.json.gz through ...-23.json.gz
    return f"https://data.gharchive.org/{dt.isoformat()}-{hour}.json.gz"


def iter_hours(start: date, end: date):
    """Yield (date, hour) pairs for every hour from start to end inclusive."""
    current = start
    while current <= end:
        for hour in range(24):
            yield current, hour
        current += timedelta(days=1)


def manifest_key(dt: date, hour: int) -> str:
    return f"{dt.isoformat()}-{hour}"


# ── Manifest (tracks which hours are done) ─────────────────────────────────────

def load_manifest() -> dict:
    if MANIFEST_PATH.exists():
        return json.loads(MANIFEST_PATH.read_text())
    return {}


def save_manifest(manifest: dict) -> None:
    MANIFEST_PATH.write_text(json.dumps(manifest, indent=2))


# ── Streaming download + filter ────────────────────────────────────────────────

def stream_filter(url: str) -> list[dict]:
    """
    Stream-decompress a GH Archive .json.gz from url.
    Return only events whose repo.name is in WATCHED_REPOS.

    Key design: we process the file line-by-line inside the HTTP response body,
    so peak memory is O(one line) regardless of file size.
    """
    try:
        with requests.get(url, stream=True, timeout=60) as resp:
            resp.raise_for_status()
            # decode_content=True tells urllib3 to handle Content-Encoding
            # transparently (not needed here since .json.gz isn't Content-Encoded,
            # but it's defensive good practice).
            resp.raw.decode_content = True

            # gzip.open accepts any file-like object; 'rt' gives us decoded text
            # so json.loads gets a str, not bytes.
            with gzip.open(resp.raw, "rt", encoding="utf-8") as gz:
                events = []
                for line in gz:
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        # GH Archive occasionally has malformed lines — skip silently
                        continue
                    if event.get("repo", {}).get("name") in WATCHED_REPOS:
                        events.append(event)
                return events

    except requests.HTTPError as e:
        if e.response.status_code == 404:
            # Normal at the trailing edge: future hours don't exist yet
            return []
        raise


# ── Local cache ────────────────────────────────────────────────────────────────

def save_filtered(events: list[dict], dt: date, hour: int) -> None:
    """
    Save the filtered events for this hour as a .jsonl file (one JSON object per line).
    This gives us a tiny local cache (~KB not ~MB) without storing the full raw file.
    """
    FILTERED_DIR.mkdir(parents=True, exist_ok=True)
    path = FILTERED_DIR / f"{dt.isoformat()}-{hour}.jsonl"
    with path.open("w") as f:
        for event in events:
            f.write(json.dumps(event) + "\n")


# ── Postgres upsert ────────────────────────────────────────────────────────────

def upsert_events(conn, events: list[dict]) -> None:
    """
    Batch-insert events. ON CONFLICT (id) DO NOTHING makes this idempotent:
    re-running the script on an already-ingested hour is a no-op.
    """
    if not events:
        return

    rows = [
        (
            e["id"],
            e["type"],
            e["repo"]["name"],
            e.get("actor", {}).get("login"),
            e["created_at"],
            # psycopg needs a plain string for JSONB; it won't auto-serialize dicts
            json.dumps(e.get("payload", {})),
        )
        for e in events
    ]

    with conn.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO events (id, type, repo_name, actor_login, created_at, payload)
            VALUES (%s, %s, %s, %s, %s, %s::jsonb)
            ON CONFLICT (id) DO NOTHING
            """,
            rows,
        )
    conn.commit()


# ── CLI entry point ────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Ingest GH Archive events for watched repos")
    parser.add_argument("--start", required=True, metavar="YYYY-MM-DD", help="First date to ingest")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--end",  metavar="YYYY-MM-DD", help="Last date to ingest (inclusive)")
    group.add_argument("--days", type=int,             help="Number of days starting from --start")
    args = parser.parse_args()

    start = date.fromisoformat(args.start)
    if args.end:
        end = date.fromisoformat(args.end)
    elif args.days:
        end = start + timedelta(days=args.days - 1)
    else:
        end = date.today()

    manifest = load_manifest()
    conn = get_connection()
    total_kept = 0
    hours_done = 0

    for dt, hour in iter_hours(start, end):
        key = manifest_key(dt, hour)

        if manifest.get(key, {}).get("ingested"):
            print(f"  skip  {key}")
            continue

        url = gharchive_url(dt, hour)
        print(f"  fetch {key} ... ", end="", flush=True)
        events = stream_filter(url)
        print(f"{len(events)} kept")

        save_filtered(events, dt, hour)
        upsert_events(conn, events)

        manifest[key] = {"ingested": True, "events_kept": len(events)}
        save_manifest(manifest)  # write after each hour so Ctrl-C doesn't lose progress

        total_kept += len(events)
        hours_done += 1

    conn.close()
    print(f"\nDone: {hours_done} hours processed, {total_kept} total events ingested.")


if __name__ == "__main__":
    main()
