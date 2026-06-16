"""
Re-ingest all events from data/filtered/*.jsonl into Postgres.

Reads the pre-filtered .jsonl files (one JSON event per line) that were
saved by the processor during the original ingestion. Skips raw downloads
entirely, making this much faster than a full re-download.

Usage:
    python -m repopulse.reingest_filtered
    python -m repopulse.reingest_filtered --start 2025-05-01 --end 2025-12-31
"""

import argparse
import json
from datetime import date, timedelta
from pathlib import Path

from repopulse.db import get_connection
from repopulse.manifest import load_manifest, save_manifest, iter_hours, manifest_key
from repopulse.processor import upsert_events

PROJECT_ROOT  = Path(__file__).resolve().parent.parent
DATA_DIR      = PROJECT_ROOT / "data"
FILTERED_DIR  = DATA_DIR / "filtered"
MANIFEST_PATH = DATA_DIR / "manifest.json"


def load_filtered(path: Path) -> list[dict]:
    events = []
    with path.open() as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    return events


def reingest(start: date, end: date) -> int:
    manifest = load_manifest(MANIFEST_PATH)
    conn = get_connection()
    total = 0

    for dt, hour in iter_hours(start, end):
        key = manifest_key(dt, hour)
        filtered = FILTERED_DIR / f"{dt.isoformat()}-{hour}.jsonl"
        if not filtered.exists():
            continue

        print(f"  reingest {key} ... ", end="", flush=True)
        events = load_filtered(filtered)
        upsert_events(conn, events)
        manifest.setdefault(key, {})["ingested"] = True
        save_manifest(MANIFEST_PATH, manifest)
        total += len(events)
        print(f"{len(events)} events")

    conn.close()
    return total


def main() -> None:
    parser = argparse.ArgumentParser(description="Re-ingest from data/filtered/ into Postgres")
    parser.add_argument("--start", default="2025-05-01", metavar="YYYY-MM-DD")
    parser.add_argument("--end",   default=date.today().isoformat(), metavar="YYYY-MM-DD")
    args = parser.parse_args()

    start = date.fromisoformat(args.start)
    end   = date.fromisoformat(args.end)
    print(f"Re-ingesting {start} → {end} from data/filtered/")
    total = reingest(start, end)
    print(f"\nDone: {total} total events re-ingested.")


if __name__ == "__main__":
    main()
