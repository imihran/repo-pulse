"""
Chunk artifact text and embed into pgvector (artifact_chunks table).

For each artifact in github_artifacts that has no chunks yet, this script:
  1. Splits the text into overlapping chunks (~500 tokens each)
  2. Prepends a metadata header to each chunk so the embedding knows the context
  3. Calls OpenAI text-embedding-3-small in batches
  4. Stores the (text, vector) pairs in artifact_chunks

Chunking is necessary because embedding models have token limits (~8k for
text-embedding-3-small). A long PR with many comments must be split before
it can be embedded. Overlapping chunks prevent context loss at boundaries.

Limitation (documented): this is vector-only retrieval. BM25/hybrid search
is deferred to a later slice — documented as a known limitation.

Usage:
    python -m repopulse.embedder --repo langchain-ai/langchain
    python -m repopulse.embedder --repo langchain-ai/langchain --limit 50
"""

import argparse

from openai import OpenAI
from dotenv import load_dotenv

from repopulse.db import get_connection

load_dotenv()

EMBEDDING_MODEL = "text-embedding-3-small"
EMBEDDING_DIMS  = 1536   # must match the vector(1536) column in artifact_chunks

# ~2000 chars ≈ ~500 tokens for English text — well within the 8192-token model limit.
# Overlap ensures a sentence cut at a chunk boundary still appears in the next chunk.
CHUNK_SIZE    = 2000
CHUNK_OVERLAP = 200

# OpenAI supports up to 2048 inputs per embedding request.
# We use 64 to keep request sizes manageable.
EMBED_BATCH_SIZE = 64


# ── Chunking ───────────────────────────────────────────────────────────────────

def chunk_text(text: str, metadata_header: str) -> list[str]:
    """
    Split text into overlapping fixed-size chunks.
    Each chunk is prefixed with a metadata header (repo, type, number, title)
    so the embedding encodes the artifact's identity even in later chunks.

    Example header: "[PULL_REQUEST #1234] Fix memory leak in tokenizer"
    """
    chunks = []
    start  = 0
    while start < len(text):
        end   = start + CHUNK_SIZE
        chunk = f"{metadata_header}\n\n{text[start:end]}"
        chunks.append(chunk)
        if end >= len(text):
            break
        start += CHUNK_SIZE - CHUNK_OVERLAP   # step forward with overlap
    return chunks


# ── Database ───────────────────────────────────────────────────────────────────

def get_unenriched(conn, repo_name: str, limit: int) -> list[dict]:
    """
    Return artifacts that have no rows in artifact_chunks yet.
    These are the ones we need to embed.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT a.id, a.repo_name, a.type, a.number, a.title, a.body, a.url
            FROM github_artifacts a
            WHERE a.repo_name = %s
              AND NOT EXISTS (
                  SELECT 1 FROM artifact_chunks c WHERE c.artifact_id = a.id
              )
            ORDER BY a.number DESC
            LIMIT %s
            """,
            (repo_name, limit),
        )
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]


def store_chunks(conn, artifact: dict, chunks: list[str], embeddings: list[list[float]]) -> None:
    """
    Insert (text, vector) pairs for one artifact.
    ON CONFLICT DO NOTHING makes this safe to re-run.
    """
    rows = [
        (
            artifact["id"],
            i,
            chunk,
            # pgvector reads vectors as the string '[0.1, 0.2, ...]'
            str(embedding),
            artifact["repo_name"],
            artifact["type"],
            artifact.get("url", ""),
        )
        for i, (chunk, embedding) in enumerate(zip(chunks, embeddings))
    ]

    with conn.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO artifact_chunks
                (artifact_id, chunk_index, text, embedding,
                 repo_name, artifact_type, artifact_url)
            VALUES (%s, %s, %s, %s::vector, %s, %s, %s)
            ON CONFLICT (artifact_id, chunk_index) DO NOTHING
            """,
            rows,
        )
    conn.commit()


# ── Embedding ──────────────────────────────────────────────────────────────────

def embed_batch(client: OpenAI, texts: list[str]) -> list[list[float]]:
    """
    Call OpenAI embeddings API for a batch of texts.
    Returns one 1536-dim vector per input text.
    """
    response = client.embeddings.create(
        input=texts,
        model=EMBEDDING_MODEL,
        dimensions=EMBEDDING_DIMS,
    )
    # response.data is ordered to match input order
    return [item.embedding for item in response.data]


# ── CLI ────────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Embed artifact chunks into pgvector")
    parser.add_argument("--repo",  required=True, help="owner/repo to embed")
    parser.add_argument("--limit", type=int, default=100,
                        help="Max artifacts to embed in one run (default 100)")
    args = parser.parse_args()

    client = OpenAI()   # reads OPENAI_API_KEY from environment
    conn   = get_connection()

    artifacts = get_unenriched(conn, args.repo, args.limit)
    print(f"Found {len(artifacts)} artifacts to embed for {args.repo}")

    total_chunks = 0

    for artifact in artifacts:
        text = (artifact.get("body") or "").strip()
        if not text:
            print(f"  skip  #{artifact['number']} (empty body)")
            continue

        header = f"[{artifact['type'].upper()} #{artifact['number']}] {artifact['title']}"
        chunks = chunk_text(text, header)

        print(f"  embed #{artifact['number']:5d} — {len(chunks)} chunk(s) ... ", end="", flush=True)

        # Process in batches to avoid hitting request size limits
        all_embeddings = []
        for i in range(0, len(chunks), EMBED_BATCH_SIZE):
            batch      = chunks[i : i + EMBED_BATCH_SIZE]
            embeddings = embed_batch(client, batch)
            all_embeddings.extend(embeddings)

        store_chunks(conn, artifact, chunks, all_embeddings)
        total_chunks += len(chunks)
        print("done")

    conn.close()
    print(f"\nDone: {total_chunks} chunks embedded across {len(artifacts)} artifacts.")

    # Remind about the HNSW index — should be built after the first batch of embeddings
    print("\nNext: build the vector index for fast similarity search:")
    print("  make psql")
    print("  CREATE INDEX ON artifact_chunks USING hnsw (embedding vector_cosine_ops);")


if __name__ == "__main__":
    main()
