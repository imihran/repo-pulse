-- Stage 10 / Slice 10: BM25 hybrid retrieval
-- Adds a GIN index on artifact_chunks for PostgreSQL full-text search.
-- This enables the hybrid (vector + BM25) path in semantic_search.
-- The index expression matches the query: to_tsvector('english', text).
--
-- Apply with:
--   make migrate-bm25
-- or manually:
--   docker compose exec -T db psql -U $POSTGRES_USER -d $POSTGRES_DB < infra/05_add_bm25_index.sql

CREATE INDEX IF NOT EXISTS artifact_chunks_fts_idx
    ON artifact_chunks
    USING GIN (to_tsvector('english', text));
