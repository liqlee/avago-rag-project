import logging

import httpx

from .config import settings
from .models import Chunk

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = (
    "You are a maintenance assistant. Answer questions using ONLY the provided "
    "manual excerpts. For every claim, cite the source manual, section, and page "
    "number in brackets.\n\n"
    "RULES:\n"
    "- If the excerpts do not contain the answer, say: \"I could not find this "
    "information in the available manuals. Please consult the original "
    "documentation or a senior technician.\"\n"
    "- NEVER guess or estimate numerical specifications (torque values, "
    "pressures, clearances, temperatures).\n"
    "- When listing procedure steps, preserve the original order exactly.\n"
    "- Always include any WARNING or CAUTION statements associated with "
    "a referenced procedure."
)

QUERY_REWRITE_PROMPT = (
    "Rewrite the following maintenance question to be more specific and "
    "searchable. Expand abbreviations, add implied equipment context, and make "
    "it suitable for searching technical maintenance manuals. Return only the "
    "rewritten query, nothing else.\n\n"
    "Original: {query}\n"
    "Rewritten:"
)


def rewrite_query(query: str) -> str:
    """Expand and clarify the query for better retrieval."""
    if not settings.QUERY_REWRITE_ENABLED:
        return query

    try:
        response = httpx.post(
            f"{settings.VLLM_BASE_URL}/v1/chat/completions",
            json={
                "model": settings.MODEL_NAME,
                "messages": [
                    {"role": "user", "content": QUERY_REWRITE_PROMPT.format(query=query)},
                ],
                "max_tokens": 100,
                "temperature": 0.0,
            },
            timeout=15.0,
        )
        response.raise_for_status()
        rewritten = response.json()["choices"][0]["message"]["content"].strip()
        logger.info("Query rewritten: '%s' → '%s'", query[:80], rewritten[:80])
        return rewritten
    except Exception:
        logger.warning("Query rewrite failed, using original", exc_info=True)
        return query


def build_context(chunks: list[Chunk]) -> str:
    """Format retrieved chunks as numbered source blocks with metadata."""
    if not chunks:
        return ""

    sections = []
    for i, chunk in enumerate(chunks, 1):
        meta = chunk.metadata
        parts = []
        if meta.get("manual_title"):
            parts.append(meta["manual_title"])
        if meta.get("section_path"):
            parts.append(meta["section_path"])
        if meta.get("page_range"):
            parts.append(f"pp. {meta['page_range']}")

        source_label = " > ".join(parts) if parts else f"Source {i}"
        sections.append(f"[Source {i}: {source_label}]\n{chunk.text}")

    return "\n\n---\n\n".join(sections)


def generate_answer(
    user_query: str,
    chunks: list[Chunk],
    chat_history: list[dict],
) -> tuple[str, str]:
    """Call vLLM with retrieved context injected.

    Returns (answer_text, context_text).  Context is passed to Guardian.
    """
    context = build_context(chunks)

    messages: list[dict] = [{"role": "system", "content": SYSTEM_PROMPT}]

    if context:
        messages.append({
            "role": "system",
            "content": (
                "The following manual excerpts are relevant to the question:"
                f"\n\n{context}"
            ),
        })

    history = [m for m in chat_history if m["role"] != "system"]
    for msg in history[-6:]:
        messages.append({"role": msg["role"], "content": msg["content"]})

    if not messages or messages[-1]["role"] != "user":
        messages.append({"role": "user", "content": user_query})

    response = httpx.post(
        f"{settings.VLLM_BASE_URL}/v1/chat/completions",
        json={
            "model": settings.MODEL_NAME,
            "messages": messages,
            "max_tokens": 2048,
            "temperature": 0.3,
        },
        timeout=120.0,
    )
    response.raise_for_status()
    answer = response.json()["choices"][0]["message"]["content"]
    return answer, context
