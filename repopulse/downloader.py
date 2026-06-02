"""
Download raw GH Archive files to data/raw/.

Writes one .json.gz per hour (20–60 MB each).
Does NOT touch the database — that's processor.py's job.

The manifest tracks which hours are downloaded so re-running is safe
and interrupted downloads resume from where they stopped.

Usage:
    python -m repopulse.downloader --start 2025-03-01 --end 2025-04-30
    python -m repopulse.downloader --start 2025-03-01 --days 7
"""

import argparse
import time
from datetime import date, timedelta
from pathlib import Path

import requests

PROJECT_ROOT  = Path(__file__).resolve().parent.parent
DATA_DIR      = PROJECT_ROOT / "data"
RAW_DIR       = DATA_DIR / "raw"

# Manifest lives in data/ and is shared with processor.py
MANIFEST_PATH = DATA_DIR / "manifest.json"

# Local import — manifest helpers are shared
from repopulse.manifest import load_manifest, save_manifest, iter_hours, manifest_key


def gharchive_url(dt: date, hour: int) -> str:
    return f"https://data.gharchive.org/{dt.isoformat()}-{hour}.json.gz"


def raw_path(dt: date, hour: int) -> Path:
    return RAW_DIR / f"{dt.isoformat()}-{hour}.json.gz"


def download_hour(dt: date, hour: int, retries: int = 3) -> bool:
    """
    Download one hour's GH Archive file to data/raw/.
    Returns True on success, False if the file doesn't exist (future hour / 404).
    Retries with exponential backoff on network errors.
    Deletes partial files before retrying so we never process a corrupt download.
    """
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    url  = gharchive_url(dt, hour)
    dest = raw_path(dt, hour)

    for attempt in range(retries):
        try:
            # stream=True + iter_content writes in 256 KB chunks so we never
            # load the full 20–60 MB file into memory at once.
            with requests.get(url, stream=True, timeout=(5, 120)) as resp:
                resp.raise_for_status()
                with dest.open("wb") as f:
                    for chunk in resp.iter_content(chunk_size=256 * 1024):
                        f.write(chunk)
            return True

        except requests.HTTPError as e:
            if e.response.status_code == 404:
                return False  # future hour — not an error
            raise

        except Exception as e:
            if dest.exists():
                dest.unlink()  # wipe partial file before retry
            if attempt < retries - 1:
                wait = 2 ** attempt  # 1s, 2s, 4s
                print(f"  [retry {attempt + 1}/{retries}] {e} — waiting {wait}s")
                time.sleep(wait)
            else:
                raise  # give up after all retries


def download_range(start: date, end: date) -> int:
    """
    Download all hourly GH Archive files for start..end inclusive.
    Idempotent: skips hours that are already on disk.
    Returns the number of files actually downloaded.
    """
    manifest   = load_manifest(MANIFEST_PATH)
    downloaded = 0

    for dt, hour in iter_hours(start, end):
        key  = manifest_key(dt, hour)
        dest = raw_path(dt, hour)

        if manifest.get(key, {}).get("downloaded") and dest.exists():
            print(f"  skip  {key}")
            continue

        print(f"  download {key} ... ", end="", flush=True)
        ok = download_hour(dt, hour)

        if not ok:
            print("404 (future hour)")
            continue

        manifest.setdefault(key, {})["downloaded"] = True
        save_manifest(MANIFEST_PATH, manifest)
        downloaded += 1
        size_mb = dest.stat().st_size / 1_000_000
        print(f"done ({size_mb:.0f} MB)")

    return downloaded


def main() -> None:
    parser = argparse.ArgumentParser(description="Download GH Archive files to data/raw/")
    parser.add_argument("--start", required=True, metavar="YYYY-MM-DD")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--end",  metavar="YYYY-MM-DD")
    group.add_argument("--days", type=int)
    args = parser.parse_args()

    start = date.fromisoformat(args.start)
    end   = date.fromisoformat(args.end) if args.end else (
            start + timedelta(days=args.days - 1) if args.days else date.today()
    )

    downloaded = download_range(start, end)
    print(f"\nDone: {downloaded} files downloaded to {RAW_DIR}/")


if __name__ == "__main__":
    main()
