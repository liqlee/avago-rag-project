import logging

import httpx

from .config import settings

logger = logging.getLogger(__name__)

MAX_CHARS_PER_CHUNK = 8000


def _truncate(text: str) -> str:
    if len(text) <= MAX_CHARS_PER_CHUNK:
        return text
    logger.warning("  Truncating chunk from %d to %d chars", len(text), MAX_CHARS_PER_CHUNK)
    return text[:MAX_CHARS_PER_CHUNK]


def _embed_one(text: str) -> list[float]:
    response = httpx.post(
        f"{settings.EMBEDDING_URL}/embed",
        json={"inputs": [_truncate(text)]},
        timeout=120.0,
    )
    response.raise_for_status()
    return response.json()[0]


def embed_batch(texts: list[str]) -> list[list[float]]:
    all_vectors: list[list[float]] = []
    batch_size = settings.EMBEDDING_BATCH_SIZE

    for i in range(0, len(texts), batch_size):
        batch = [_truncate(t) for t in texts[i : i + batch_size]]
        logger.info(
            "  Embedding batch %d-%d of %d",
            i + 1,
            min(i + batch_size, len(texts)),
            len(texts),
        )
        try:
            response = httpx.post(
                f"{settings.EMBEDDING_URL}/embed",
                json={"inputs": batch},
                timeout=120.0,
            )
            response.raise_for_status()
            all_vectors.extend(response.json())
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 413:
                logger.warning("  413 Payload Too Large, falling back to one-at-a-time")
                for t in batch:
                    all_vectors.append(_embed_one(t))
            else:
                raise

    return all_vectors
