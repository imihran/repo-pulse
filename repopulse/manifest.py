"""
Shared manifest helpers used by downloader.py and processor.py.

The manifest is a JSON file at data/manifest.json that tracks, per hour:
  - downloaded: raw .json.gz is on disk
  - ingested:   events have been pushed to Postgres
  - events_kept: how many matched events were found

Example entry:
  "2025-03-01-0": {"downloaded": true, "ingested": true, "events_kept": 25}
"""

import json
from datetime import date, timedelta
from pathlib import Path


def manifest_key(dt: date, hour: int) -> str:
    return f"{dt.isoformat()}-{hour}"


def iter_hours(start: date, end: date):
    """Yield (date, hour) pairs for every hour from start to end inclusive."""
    current = start
    while current <= end:
        for hour in range(24):
            yield current, hour
        current += timedelta(days=1)


def load_manifest(path: Path) -> dict:
    if path.exists():
        return json.loads(path.read_text())
    return {}


def save_manifest(path: Path, manifest: dict) -> None:
    # Write after every hour so Ctrl-C / crashes don't lose progress
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest, indent=2))
