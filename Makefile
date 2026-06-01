# Makefile — convenience shortcuts for the local dev workflow.
# Run any target with: make <target>   e.g. `make up`
.PHONY: up down psql logs migrate venv download process detect enrich embed investigate eval-prepare eval test export serve

# ── Docker ────────────────────────────────────────────────────────────────────

# Start all services in detached (background) mode.
up:
	docker compose up -d

# Stop all services. Data in the named volume survives.
# Use `docker compose down -v` directly for a full wipe (deletes all data).
down:
	docker compose down

# Open an interactive psql shell inside the running db container.
psql:
	docker compose exec db psql -U $${POSTGRES_USER} -d $${POSTGRES_DB}

# Stream Postgres logs. Ctrl-C to stop.
logs:
	docker compose logs -f db

# ── Schema ────────────────────────────────────────────────────────────────────

# Apply (or re-apply) the schema. Safe to run multiple times — all statements
# use IF NOT EXISTS or ON CONFLICT DO NOTHING.
migrate:
	docker compose exec -T db psql -U $${POSTGRES_USER} -d $${POSTGRES_DB} < infra/02_schema.sql

# ── Python ────────────────────────────────────────────────────────────────────

# Create a virtual environment and install the package in editable mode.
# Run once after cloning: make venv && source .venv/bin/activate
venv:
	python3 -m venv .venv
	.venv/bin/pip install -e .

# Step 1: download raw .json.gz files to data/raw/
#   make download ARGS="--start 2025-03-01 --end 2025-04-30"
download:
	python -m repopulse.downloader $(ARGS)

# Step 2: filter and push downloaded files into Postgres
#   make process ARGS="--start 2025-03-01 --end 2025-04-30"
#   make process ARGS="--start 2025-03-01 --end 2025-04-30 --delete-raw"
process:
	python -m repopulse.processor $(ARGS)

# Run the detector after processing is done
#   make detect ARGS="--window-end 2025-04-30"
detect:
	python -m repopulse.detector $(ARGS)

# Fetch PR/issue text from GitHub API → github_artifacts
#   make enrich ARGS="--repo langchain-ai/langchain --start 2025-04-24 --end 2025-04-30"
enrich:
	python -m repopulse.enricher $(ARGS)

# Chunk + embed artifacts → artifact_chunks (pgvector)
#   make embed ARGS="--repo langchain-ai/langchain"
embed:
	python -m repopulse.embedder $(ARGS)

# Run the investigator agent on a detected anomaly
#   make investigate ARGS="--anomaly-id 1"
investigate:
	python -m repopulse.agent $(ARGS)

# ── Eval ──────────────────────────────────────────────────────────────────────

# Step 1: enrich + embed all golden case windows (run once)
eval-prepare:
	python -m eval.prepare

# Run guardrail tests (no DB or LLM required)
test:
	pytest tests/ -v

# Export reports from DB to static JSON for the web frontend
export:
	python -m repopulse.export

# Serve the web frontend locally for preview (Python's built-in server)
serve:
	cd web && python3 -m http.server 8080

# Step 2: run agent on all golden cases and print scores
#   make eval
#   make eval ARGS="--judge"           # add LLM groundedness scoring
#   make eval ARGS="--skip-existing"   # re-use already-stored reports
eval:
	python -m eval.evaluator $(ARGS)
