"""
RepoPulse daily ingestion + investigation pipeline.

Schedule: 6 AM UTC daily. Airflow's execution date (ds) is the logical
start of the data interval — for a daily schedule this equals yesterday,
which is what we want: yesterday's GH Archive data is fully available by 6 AM.

Pipeline (one task per stage, left to right dependencies):

  download → process → detect → enrich → embed → investigate → export

Each task is idempotent — safe to re-trigger on the same date.
The repopulse package must be installed in the same Python environment
that Airflow uses: `pip install -e .` from the project root.

Setup (first time):
    pip install "apache-airflow>=2.9"
    export AIRFLOW_HOME=$(pwd)/.airflow
    airflow db migrate
    airflow users create -u admin -p admin -r Admin -f Admin -l User -e admin@local
    airflow standalone          # starts both scheduler and webserver
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta

from airflow import DAG
from airflow.operators.python import PythonOperator

log = logging.getLogger(__name__)

REPOS = [
    "vllm-project/vllm",
    "langchain-ai/langchain",
    "dbt-labs/dbt-core",
]

MAX_INVESTIGATIONS_PER_RUN = 5   # cap LLM cost per daily run (~$0.05–0.10)


# ── Task callables ─────────────────────────────────────────────────────────────
# Each function is self-contained: imports happen inside to avoid top-level
# import failures when Airflow loads the DAG file on startup.

def _download(ds: str, **_) -> None:
    """Download 24 hourly GH Archive files for the execution date (yesterday)."""
    from datetime import date
    from repopulse.downloader import download_range

    target = date.fromisoformat(ds)
    log.info("Downloading GH Archive for %s", target)
    n = download_range(target, target)
    log.info("Downloaded %d files for %s", n, target)


def _process(ds: str, **_) -> None:
    """Filter downloaded files and upsert events into Postgres; delete raw files."""
    from datetime import date
    from repopulse.processor import process_range

    target = date.fromisoformat(ds)
    log.info("Processing events for %s", target)
    n = process_range(target, target, delete_raw=True)
    log.info("Ingested %d events for %s", n, target)


def _detect(ds: str, **_) -> None:
    """Aggregate daily metrics and run anomaly detection for the execution date."""
    from datetime import date
    from repopulse.db import get_connection
    from repopulse.detector import aggregate_metrics, run_detection, insert_anomalies

    window_end = date.fromisoformat(ds)
    conn = get_connection()
    try:
        log.info("Aggregating metrics for %s", window_end)
        aggregate_metrics(conn, window_end, window_end)

        log.info("Running detection with window_end=%s", window_end)
        anomalies = run_detection(conn, window_end)
        insert_anomalies(conn, anomalies)
        log.info("%d new anomaly/anomalies detected", len(anomalies))
    finally:
        conn.close()


def _enrich(**_) -> None:
    """
    Fetch PR/issue text from GitHub API for any anomaly window not yet enriched.
    Uses both the events table and the GitHub search API (for PRs opened before
    our event window).
    """
    from repopulse.db import get_connection
    from repopulse.enricher import (
        GITHUB_TOKEN, fetch_pr, fetch_comments, build_text,
        get_pr_numbers, fetch_merged_prs, upsert_artifact,
    )

    if not GITHUB_TOKEN:
        log.warning("GITHUB_TOKEN not set — skipping enrichment")
        return

    conn = get_connection()
    try:
        with conn.cursor() as cur:
            # Anomalies whose window has no enriched artifacts yet
            cur.execute("""
                SELECT DISTINCT a.repo_name, a.window_start, a.window_end
                FROM anomalies a
                WHERE NOT EXISTS (
                    SELECT 1 FROM github_artifacts g
                    WHERE g.repo_name = a.repo_name
                      AND g.created_at::date BETWEEN a.window_start AND a.window_end
                )
                ORDER BY a.window_start
            """)
            windows = cur.fetchall()

        log.info("%d unenriched anomaly window(s) to process", len(windows))

        for repo_name, window_start, window_end in windows:
            owner, repo = repo_name.split("/")
            log.info("Enriching %s %s → %s", repo_name, window_start, window_end)

            from_events = get_pr_numbers(conn, repo_name, window_start, window_end, limit=30)
            from_search = fetch_merged_prs(owner, repo, window_start, window_end, limit=30)
            pr_numbers  = sorted(set(from_events) | set(from_search), reverse=True)[:30]

            enriched = 0
            for number in pr_numbers:
                pr = fetch_pr(owner, repo, number)
                if not pr:
                    continue
                comments  = fetch_comments(owner, repo, number)
                full_text = build_text(pr, comments)
                upsert_artifact(conn, repo_name, pr, full_text)
                enriched += 1
                time.sleep(0.2)

            log.info("Enriched %d PRs for %s", enriched, repo_name)
    finally:
        conn.close()


def _embed(**_) -> None:
    """Chunk and embed all unenriched artifacts into pgvector."""
    from openai import OpenAI
    from repopulse.db import get_connection
    from repopulse.embedder import get_unenriched, embed_batch, store_chunks, chunk_text

    conn   = get_connection()
    client = OpenAI()
    total  = 0
    try:
        for repo in REPOS:
            artifacts = get_unenriched(conn, repo, limit=200)
            if not artifacts:
                continue
            log.info("Embedding %d artifact(s) for %s", len(artifacts), repo)
            for artifact in artifacts:
                text = (artifact.get("body") or "").strip()
                if not text:
                    continue
                header = f"[{artifact['type'].upper()} #{artifact['number']}] {artifact['title']}"
                chunks = chunk_text(text, header)
                artifact["repo_name"] = repo
                embeddings = embed_batch(client, chunks)
                store_chunks(conn, artifact, chunks, embeddings)
                total += len(chunks)
    finally:
        conn.close()
    log.info("Embedded %d total chunk(s)", total)


def _investigate(**_) -> None:
    """
    Run the investigator agent on anomalies that have no report yet.
    Capped at MAX_INVESTIGATIONS_PER_RUN to control daily LLM cost.
    """
    from repopulse.db import get_connection
    from repopulse.agent import investigate

    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT a.id
                FROM anomalies a
                WHERE NOT EXISTS (
                    SELECT 1 FROM investigation_reports r WHERE r.anomaly_id = a.id
                )
                ORDER BY a.z_score DESC
                LIMIT %s
            """, (MAX_INVESTIGATIONS_PER_RUN,))
            anomaly_ids = [row[0] for row in cur.fetchall()]
    finally:
        conn.close()

    log.info("Investigating %d anomaly/anomalies (cap=%d)", len(anomaly_ids), MAX_INVESTIGATIONS_PER_RUN)
    for anomaly_id in anomaly_ids:
        try:
            report = investigate(anomaly_id)
            log.info("  anomaly_id=%d → confidence=%s", anomaly_id, report.confidence)
        except Exception:
            log.exception("  anomaly_id=%d investigation failed", anomaly_id)


def _export(**_) -> None:
    """Regenerate web/data/*.json from the database."""
    from repopulse.export import main as export_main

    log.info("Exporting reports to web/data/")
    export_main()
    log.info("Export complete")


# ── DAG definition ─────────────────────────────────────────────────────────────

with DAG(
    dag_id="repopulse_daily",
    description="Daily GH Archive ingestion → anomaly detection → investigation → export",
    schedule="0 6 * * *",          # 6 AM UTC — yesterday's data is complete by then
    start_date=datetime(2025, 3, 1),
    catchup=False,                  # don't backfill historical dates on first run
    max_active_runs=1,              # never overlap two daily runs
    default_args={
        "owner":           "repopulse",
        "retries":         2,
        "retry_delay":     timedelta(minutes=5),
        "email_on_failure": False,
    },
    tags=["repopulse", "ingestion"],
) as dag:

    download = PythonOperator(
        task_id="download",
        python_callable=_download,
        doc_md="Download 24 hourly GH Archive files for yesterday.",
    )

    process = PythonOperator(
        task_id="process",
        python_callable=_process,
        doc_md="Filter events and upsert into Postgres; delete raw files.",
    )

    detect = PythonOperator(
        task_id="detect",
        python_callable=_detect,
        doc_md="Aggregate daily metrics and run MAD z-score anomaly detection.",
    )

    enrich = PythonOperator(
        task_id="enrich",
        python_callable=_enrich,
        doc_md="Fetch PR/issue text from GitHub API for new anomaly windows.",
    )

    embed = PythonOperator(
        task_id="embed",
        python_callable=_embed,
        doc_md="Chunk and embed new artifacts into pgvector.",
    )

    investigate = PythonOperator(
        task_id="investigate",
        python_callable=_investigate,
        doc_md=f"Run investigator agent on top-z anomalies (cap={MAX_INVESTIGATIONS_PER_RUN}/day).",
    )

    export = PythonOperator(
        task_id="export",
        python_callable=_export,
        doc_md="Export reports to web/data/*.json for the Cloudflare Pages frontend.",
    )

    # ── Dependency chain ───────────────────────────────────────────────────────
    download >> process >> detect >> enrich >> embed >> investigate >> export
