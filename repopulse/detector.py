"""
Slice 4: Anomaly Detector

Two jobs in one script:

  1. aggregate_metrics — roll raw events into repo_metrics_daily (one row per repo per day).
     Uses a single SQL pass with COUNT(*) FILTER (...) to compute all 8 metrics at once.

  2. run_detection — compare the last 7 days against the prior 28 using a MAD z-score.
     Any repo×metric that exceeds the threshold gets written to the anomalies table.

Usage:
    python -m repopulse.detector --window-end 2025-04-05
    python -m repopulse.detector --window-end 2025-04-05 --z-threshold 2.5
    python -m repopulse.detector --window-end 2025-04-05 --skip-metrics
"""

import argparse
import statistics
from datetime import date, timedelta

from repopulse.db import get_connection


# ── Anomaly check definitions ─────────────────────────────────────────────────
# Each entry describes one thing the detector looks for.
# min_baseline: if the prior-28d median is below this, skip — too little activity
# to distinguish signal from noise (e.g. a repo that gets 0 stars/day most days).

ANOMALY_CHECKS = [
    {
        "anomaly_type": "issue_spike",
        "metric_col":   "issue_opened",
        "min_baseline":  2.0,   # skip repos averaging < 2 issues/day
    },
    {
        "anomaly_type": "pr_slowdown",
        "metric_col":   "pr_median_merge_hours",
        "min_baseline":  1.0,   # skip repos with almost no merges
    },
    {
        "anomaly_type": "star_spike",
        "metric_col":   "star_count",
        "min_baseline":  5.0,   # skip repos with near-zero organic stars
    },
]

DEFAULT_Z_THRESHOLD = 3.5


# ── Job 1: Aggregate events → repo_metrics_daily ──────────────────────────────

def aggregate_metrics(conn, start_date: date, end_date: date) -> None:
    """
    Compute daily metric counts from the events table and upsert into repo_metrics_daily.

    The FILTER clause on COUNT() is the key pattern here:
        COUNT(*) FILTER (WHERE type = 'WatchEvent')
    does the same thing as a WHERE clause, but inside an aggregate — so we get
    all 8 metrics in a single pass over the events table instead of 8 queries.

    ON CONFLICT ... DO UPDATE makes it safe to re-run: if a row already exists
    for (repo_name, date), it gets refreshed rather than causing an error.
    This matters if new events arrive for a day we've already aggregated.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            -- GH Archive changed its payload format: newer events only include
            -- pull_request.id, not the full PR object. We handle both formats:
            --   Old (pre-2026): merged flag and timestamps in pull_request object
            --   New (2026+):    only pull_request.id; use event timestamps + self-join
            --                   for cycle time; treat action=closed as merged

            WITH pr_cycles AS (
                -- Compute PR cycle time (open → close) by joining opened and
                -- closed events on the same PR number within the same repo.
                -- This works for both old and new payload formats.
                SELECT
                    c.repo_name,
                    (c.created_at AT TIME ZONE 'UTC')::date AS close_date,
                    PERCENTILE_CONT(0.5) WITHIN GROUP (
                        ORDER BY EXTRACT(EPOCH FROM (c.created_at - o.created_at)) / 3600.0
                    ) AS median_hours
                FROM events c
                JOIN events o
                  ON  o.repo_name           = c.repo_name
                  AND o.payload->>'number'  = c.payload->>'number'
                  AND o.type                = 'PullRequestEvent'
                  AND o.payload->>'action'  = 'opened'
                  AND o.created_at          < c.created_at
                WHERE c.type               = 'PullRequestEvent'
                  AND c.payload->>'action' = 'closed'
                  AND (c.created_at AT TIME ZONE 'UTC')::date
                      BETWEEN %(start_date)s AND %(end_date)s
                GROUP BY c.repo_name, (c.created_at AT TIME ZONE 'UTC')::date
            ),
            daily AS (
                SELECT
                    repo_name,
                    (created_at AT TIME ZONE 'UTC')::date AS date,

                    COUNT(*) FILTER (WHERE type = 'WatchEvent')
                        AS star_count,

                    COUNT(*) FILTER (WHERE type = 'ForkEvent')
                        AS fork_count,

                    COUNT(*) FILTER (WHERE type = 'IssuesEvent'
                                       AND payload->>'action' = 'opened')
                        AS issue_opened,

                    COUNT(*) FILTER (WHERE type = 'IssuesEvent'
                                       AND payload->>'action' = 'closed')
                        AS issue_closed,

                    COUNT(*) FILTER (WHERE type = 'PullRequestEvent'
                                       AND payload->>'action' = 'opened')
                        AS pr_opened,

                    -- Old format: count only confirmed merges.
                    -- New format: merged flag absent, so count all closes as merges
                    -- (best available proxy when payload is truncated).
                    COUNT(*) FILTER (WHERE type = 'PullRequestEvent'
                                       AND payload->>'action' = 'closed'
                                       AND COALESCE(
                                           (payload->'pull_request'->>'merged')::boolean,
                                           true  -- new format: assume closed = merged
                                       ))
                        AS pr_merged,

                    COUNT(*) FILTER (WHERE type = 'PushEvent')
                        AS commit_count

                FROM events
                WHERE (created_at AT TIME ZONE 'UTC')::date
                      BETWEEN %(start_date)s AND %(end_date)s
                GROUP BY repo_name, (created_at AT TIME ZONE 'UTC')::date
            )
            INSERT INTO repo_metrics_daily (
                repo_name, date,
                star_count, fork_count,
                issue_opened, issue_closed,
                pr_opened, pr_merged, pr_median_merge_hours,
                commit_count
            )
            SELECT
                d.repo_name,
                d.date,
                d.star_count,
                d.fork_count,
                d.issue_opened,
                d.issue_closed,
                d.pr_opened,
                d.pr_merged,
                pc.median_hours   AS pr_median_merge_hours,
                d.commit_count
            FROM daily d
            LEFT JOIN pr_cycles pc
              ON pc.repo_name  = d.repo_name
             AND pc.close_date = d.date

            ON CONFLICT (repo_name, date) DO UPDATE SET
                star_count            = EXCLUDED.star_count,
                fork_count            = EXCLUDED.fork_count,
                issue_opened          = EXCLUDED.issue_opened,
                issue_closed          = EXCLUDED.issue_closed,
                pr_opened             = EXCLUDED.pr_opened,
                pr_merged             = EXCLUDED.pr_merged,
                pr_median_merge_hours = EXCLUDED.pr_median_merge_hours,
                commit_count          = EXCLUDED.commit_count
            """,
            {"start_date": start_date, "end_date": end_date},
        )
    conn.commit()
    print(f"  metrics aggregated: {start_date} → {end_date}")


# ── Job 2: Detect anomalies ───────────────────────────────────────────────────

def fetch_series(
    conn,
    repo_name: str,
    metric_col: str,
    prior_start: date,
    recent_end: date,
) -> tuple[list[float], list[float]]:
    """
    Fetch 35 days of daily values for one repo×metric, split into prior and recent.

    NULLs are excluded from the query — relevant for pr_avg_merge_hours on days
    with no merges at all (which would otherwise artificially pull the baseline down).

    metric_col is always set from ANOMALY_CHECKS (never from user input), so
    the string interpolation is safe — there's no injection risk here.
    """
    # Split point: 7 days before window_end is the boundary between windows
    recent_start = recent_end - timedelta(days=6)
    prior_end    = recent_end - timedelta(days=7)

    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT date, {metric_col}
            FROM repo_metrics_daily
            WHERE repo_name = %s
              AND date BETWEEN %s AND %s
              AND {metric_col} IS NOT NULL
            ORDER BY date
            """,
            (repo_name, prior_start, recent_end),
        )
        rows = cur.fetchall()

    prior  = [float(v) for d, v in rows if d <= prior_end]
    recent = [float(v) for d, v in rows if d >= recent_start]
    return prior, recent


def mad_zscore(recent_vals: list[float], prior_vals: list[float]) -> float:
    """
    MAD z-score: how many 'robust standard deviations' the recent mean sits
    above the prior median.

    Why MAD instead of plain z-score?
      A plain z-score uses the mean as its center. If the prior 28 days
      happened to include one big spike (say a GitHub trending event), that
      spike inflates the mean and suppresses detection of future spikes.
      MAD uses the *median* instead — single outliers don't move it.

    Formula:  z = 0.6745 * (observed_mean - baseline_median) / MAD
    The 0.6745 constant scales MAD to 1 std dev for normal distributions
    (Iglewicz & Hoaglin 1993). With default threshold 3.5, this catches
    events that are very unlikely under a normal baseline.

    Edge case: MAD = 0 means all prior values are identical (e.g. exactly 10
    stars/day every day). We fall back to a ratio-based score scaled to be
    comparable to the z-score range.
    """
    if not prior_vals or not recent_vals:
        return 0.0

    baseline_median = statistics.median(prior_vals)
    mad = statistics.median([abs(x - baseline_median) for x in prior_vals])
    observed_mean = statistics.mean(recent_vals)

    if mad == 0:
        if baseline_median == 0:
            return 0.0
        # Scale ratio to roughly match z-score magnitude
        return (observed_mean - baseline_median) / baseline_median * 10

    return 0.6745 * (observed_mean - baseline_median) / mad


def run_detection(
    conn,
    window_end: date,
    z_threshold: float = DEFAULT_Z_THRESHOLD,
) -> list[dict]:
    """
    For each repo × anomaly check, compute the MAD z-score and collect
    any anomalies above the threshold.

    Detection windows (relative to window_end):
        recent : window_end-6  → window_end      (7 days)
        prior  : window_end-34 → window_end-7    (28 days)
    """
    recent_start = window_end - timedelta(days=6)
    prior_start  = window_end - timedelta(days=34)

    with conn.cursor() as cur:
        cur.execute("SELECT name FROM repos")
        repos = [row[0] for row in cur.fetchall()]

    found = []

    for repo in repos:
        for check in ANOMALY_CHECKS:
            metric_col   = check["metric_col"]
            anomaly_type = check["anomaly_type"]
            min_baseline = check["min_baseline"]

            prior, recent = fetch_series(conn, repo, metric_col, prior_start, window_end)

            if len(prior) < 7:
                # Not enough history — the detector would be guessing, not measuring
                print(f"  skip  {repo:30s} / {anomaly_type}: only {len(prior)} prior days (need 7+)")
                continue

            baseline_median = statistics.median(prior)

            if baseline_median < min_baseline:
                # Baseline activity is too low to distinguish signal from noise
                print(f"  skip  {repo:30s} / {anomaly_type}: baseline {baseline_median:.1f} < min {min_baseline}")
                continue

            z = mad_zscore(recent, prior)
            observed_mean = statistics.mean(recent) if recent else 0.0

            flag = "  ANOMALY" if z >= z_threshold else ""
            print(
                f"  check {repo:30s} / {anomaly_type:12s}: "
                f"z={z:+.2f}  baseline={baseline_median:.1f}  observed={observed_mean:.1f}"
                f"{flag}"
            )

            if z >= z_threshold:
                found.append({
                    "repo_name":      repo,
                    "anomaly_type":   anomaly_type,
                    "window_start":   recent_start,
                    "window_end":     window_end,
                    "metric_name":    metric_col,
                    "baseline_value": baseline_median,
                    "observed_value": observed_mean,
                    "z_score":        z,
                })

    return found


def insert_anomalies(conn, anomalies: list[dict]) -> None:
    if not anomalies:
        print("\nNo anomalies above threshold.")
        return
    with conn.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO anomalies
                (repo_name, anomaly_type, window_start, window_end,
                 metric_name, baseline_value, observed_value, z_score)
            VALUES
                (%(repo_name)s, %(anomaly_type)s, %(window_start)s, %(window_end)s,
                 %(metric_name)s, %(baseline_value)s, %(observed_value)s, %(z_score)s)
            """,
            anomalies,
        )
    conn.commit()
    print(f"\n{len(anomalies)} anomaly row(s) inserted into anomalies table.")


# ── CLI ───────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Aggregate metrics and detect anomalies")
    parser.add_argument(
        "--window-end",
        default=str(date.today()),
        metavar="YYYY-MM-DD",
        help="Last day of the 7-day observation window (default: today)",
    )
    parser.add_argument(
        "--z-threshold",
        type=float,
        default=DEFAULT_Z_THRESHOLD,
        help=f"MAD z-score threshold for anomaly detection (default: {DEFAULT_Z_THRESHOLD})",
    )
    parser.add_argument(
        "--skip-metrics",
        action="store_true",
        help="Skip metric aggregation and go straight to detection (repo_metrics_daily must be current)",
    )
    args = parser.parse_args()

    window_end = date.fromisoformat(args.window_end)
    conn = get_connection()

    if not args.skip_metrics:
        print("Aggregating metrics from events table...")
        with conn.cursor() as cur:
            cur.execute("SELECT MIN(created_at::date), MAX(created_at::date) FROM events")
            min_date, max_date = cur.fetchone()

        if not min_date:
            print("No events found — run `make ingest` first.")
            conn.close()
            return

        aggregate_metrics(conn, min_date, max_date)

    print(f"\nRunning detection (window_end={window_end}, threshold={args.z_threshold})...")
    anomalies = run_detection(conn, window_end, args.z_threshold)
    insert_anomalies(conn, anomalies)
    conn.close()


if __name__ == "__main__":
    main()
