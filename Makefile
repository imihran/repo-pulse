# Makefile — convenience shortcuts for the local dev workflow.
# Run any target with: make <target>   e.g. `make up`
.PHONY: up down psql logs migrate venv ingest

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

# Run the ingest script. Pass dates via ARGS, e.g.:
#   make ingest ARGS="--start 2025-03-01 --days 7"
ingest:
	python -m repopulse.ingest $(ARGS)
