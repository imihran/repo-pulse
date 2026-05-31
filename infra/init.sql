-- init.sql — runs once, automatically, when Postgres first creates the database.
-- Postgres executes every file in /docker-entrypoint-initdb.d/ in filename order.
-- This file is named 01_init.sql so it always runs first if we add more later.

-- Activate the pgvector extension in this database.
-- IF NOT EXISTS makes this safe to re-run (idempotent) — won't error if it's already on.
-- This adds the `vector` data type and distance operators (<->, <=>, <#>).
-- Without this line, any CREATE TABLE with a vector column would fail.
CREATE EXTENSION IF NOT EXISTS vector;
