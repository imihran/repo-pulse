# Makefile — convenience shortcuts so you don't have to memorise docker compose flags.
# Run any target with: make <target>   e.g. `make up`
#
# .PHONY tells make these aren't filenames — without it, make would skip the
# target if a file named "up" or "down" happened to exist in the directory.
.PHONY: up down psql logs

# Start all services in detached (background) mode.
up:
	docker compose up -d

# Stop all services. Data in the named volume survives.
# Use `docker compose down -v` directly if you want a full wipe.
down:
	docker compose down

# Open an interactive psql shell inside the running db container.
# $${POSTGRES_USER} — double $ escapes the $ for Make; the shell then expands it
# using the value loaded from .env by docker compose exec.
psql:
	docker compose exec db psql -U $${POSTGRES_USER} -d $${POSTGRES_DB}

# Stream Postgres logs to your terminal. Ctrl-C to stop.
logs:
	docker compose logs -f db
