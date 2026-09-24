import logging

import httpx
from pgvector.psycopg import register_vector
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from .config import settings
from .models import Chunk

logger = logging.getLogger(__name__)

_pool: ConnectionPool | None = None


def init_db():
    global _pool
    _pool = ConnectionPool(
        settings.get_database_url(),
        min_size=2,
        max_size=10,
        configure=_configure_connection,
        open=True,
    )
    logger.info("Database connection pool initialized")


def _configure_connection(conn):
    register_vector(conn)


def embed_query(text: str) -> list[float]:
    """Call BGE-M3 to get a 1024-dim dense embedding."""
    response = httpx.post(
        f"{settings.EMBEDDING_URL}/embed",
        json={"inputs": text},
        timeout=30.0,
    )
    response.raise_for_status()
    return response.json()[0]


def search_chunks(query_vector: list[float], top_k: int | None = None) -> list[Chunk]:
    """Cosine similarity search against pgvector."""
    if top_k is None:
        top_k = settings.RETRIEVAL_TOP_K

    vector_str = f"[{','.join(str(x) for x in query_vector)}]"

    with _pool.connection() as conn:
        conn.row_factory = dict_row
        rows = conn.execute(
            """
            SELECT id, text, chunk_type, metadata,
                   1 - (dense_vector <=> %s::vector) AS score
            FROM chunks
            WHERE dense_vector IS NOT NULL
            ORDER BY dense_vector <=> %s::vector
            LIMIT %s
            """,
            (vector_str, vector_str, top_k),
        ).fetchall()

    return [
        Chunk(
            id=row["id"],
            text=row["text"],
            chunk_type=row["chunk_type"],
            metadata=row["metadata"],
            score=float(row["score"]),
        )
        for row in rows
    ]


def rerank_chunks(
    query: str, chunks: list[Chunk], top_n: int | None = None
) -> list[Chunk]:
    """Cross-encoder re-scoring via BGE-reranker."""
    if top_n is None:
        top_n = settings.RERANK_TOP_N

    if not chunks:
        return []

    response = httpx.post(
        f"{settings.RERANKER_URL}/rerank",
        json={
            "query": query,
            "texts": [c.text for c in chunks],
        },
        timeout=30.0,
    )
    response.raise_for_status()

    scored = sorted(response.json(), key=lambda r: r["score"], reverse=True)[:top_n]
    return [
        Chunk(
            id=chunks[r["index"]].id,
            text=chunks[r["index"]].text,
            chunk_type=chunks[r["index"]].chunk_type,
            metadata=chunks[r["index"]].metadata,
            score=r["score"],
        )
        for r in scored
    ]
