"""
Process downloaded GH Archive files and push matching events into Postgres.

Reads raw .json.gz files from data/raw/, filters to watched repos,
saves a tiny .jsonl per hour to data/filtered/, and upserts into the
events table. Run downloader.py first.

Usage:
    python -m repopulse.processor --start 2025-03-01 --end 2025-04-30
    python -m repopulse.processor --start 2025-03-01 --days 7
    python -m repopulse.processor --start 2025-03-01 --days 7 --delete-raw
"""

import argparse
import gzip
import json
from datetime import date, timedelta
from pathlib import Path

from repopulse.db import get_connection
from repopulse.manifest import load_manifest, save_manifest, iter_hours, manifest_key

WATCHED_REPOS = {
    "vllm-project/vllm",
    "langchain-ai/langchain",
    "dbt-labs/dbt-core",
}

DATA_DIR      = Path("data")
RAW_DIR       = DATA_DIR / "raw"
FILTERED_DIR  = DATA_DIR / "filtered"
MANIFEST_PATH = DATA_DIR / "manifest.json"


def raw_path(dt: date, hour: int) -> Path:
    return RAW_DIR / f"{dt.isoformat()}-{hour}.json.gz"

def filtered_path(dt: date, hour: int) -> Path:
    return FILTERED_DIR / f"{dt.isoformat()}-{hour}.jsonl"


def filter_events(dt: date, hour: int) -> list[dict]:
    """
    Read the local raw file line-by-line and return only events for watched repos.
    Reading from disk (not HTTP) means no network timeouts here.
    """
    events = []
    with gzip.open(raw_path(dt, hour), "rt", encoding="utf-8") as gz:
        for line in gz:
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("repo", {}).get("name") in WATCHED_REPOS:
                events.append(event)
    return events


def save_filtered(events: list[dict], dt: date, hour: int) -> None:
    """Save matching events as a tiny .jsonl file for reference / re-processing."""
    FILTERED_DIR.mkdir(parents=True, exist_ok=True)
    with filtered_path(dt, hour).open("w") as f:
        for event in events:
            f.write(json.dumps(event) + "\n")


def upsert_events(conn, events: list[dict]) -> None:
    if not events:
        return

    rows = [
        (
            e["id"],
            e["type"],
            e["repo"]["name"],
            e.get("actor", {}).get("login"),
            e["created_at"],
            # Postgres rejects null bytes in text/JSONB — strip them.
            # GitHub issue bodies occasionally embed the  character.
            json.dumps(e.get("payload", {})).replace("\\u0000", ""),
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


def main() -> None:
    parser = argparse.ArgumentParser(description="Process downloaded GH Archive files into Postgres")
    parser.add_argument("--start", required=True, metavar="YYYY-MM-DD")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--end",  metavar="YYYY-MM-DD")
    group.add_argument("--days", type=int)
    parser.add_argument("--delete-raw", action="store_true",
                        help="Delete raw .json.gz after successful ingestion (saves ~55 GB for 60 days)")
    args = parser.parse_args()

    start = date.fromisoformat(args.start)
    end   = date.fromisoformat(args.end) if args.end else (
            start + timedelta(days=args.days - 1) if args.days else date.today()
    )

    manifest = load_manifest(MANIFEST_PATH)
    conn     = get_connection()
    total    = 0

    for dt, hour in iter_hours(start, end):
        key = manifest_key(dt, hour)

        if manifest.get(key, {}).get("ingested"):
            print(f"  skip  {key}")
            continue

        src = raw_path(dt, hour)
        if not src.exists():
            print(f"  skip  {key} (not downloaded — run downloader.py first)")
            continue

        print(f"  process {key} ... ", end="", flush=True)
        events = filter_events(dt, hour)
        save_filtered(events, dt, hour)
        upsert_events(conn, events)

        manifest.setdefault(key, {})["ingested"]    = True
        manifest[key]["events_kept"] = len(events)
        save_manifest(MANIFEST_PATH, manifest)
        total += len(events)
        print(f"{len(events)} kept")

        if args.delete_raw:
            src.unlink()

    conn.close()
    print(f"\nDone: {total} total events ingested.")


if __name__ == "__main__":
    main()
