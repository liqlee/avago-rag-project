from __future__ import annotations

import logging
from typing import TypedDict

from langgraph.graph import END, StateGraph

from .config import settings
from .generator import generate_answer, rewrite_query
from .guardian import check_groundedness
from .models import Chunk
from .retriever import embed_query, rerank_chunks, search_chunks

logger = logging.getLogger(__name__)


class RAGState(TypedDict, total=False):
    """Accumulated state for the RAG pipeline.

    Each node reads what it needs and returns a partial dict
    with the fields it produces.  LangGraph merges the updates
    into a single state object that every downstream node can access.
    """

    # Inputs (set before invocation)
    user_query: str
    chat_history: list[dict]
    model: str

    # Step 1: Query Rewrite
    rewritten_query: str

    # Step 2: Embed
    query_vector: list[float]

    # Step 3: Vector Search
    candidates: list[Chunk]

    # Step 4: Rerank
    chunks: list[Chunk]

    # Step 5: Generate
    context: str
    answer: str

    # Step 6: Guardian
    is_grounded: bool
    final_answer: str


# ── Node functions ────────────────────────────────────────────
#
# Each node receives the full accumulated state and returns a
# partial dict with only the fields it produces.  LangGraph
# merges the update into the running state automatically.


def rewrite_node(state: RAGState) -> dict:
    user_query = state["user_query"]
    rewritten = rewrite_query(user_query)
    return {"rewritten_query": rewritten}


def embed_node(state: RAGState) -> dict:
    query = state.get("rewritten_query", state["user_query"])
    try:
        vector = embed_query(query)
        return {"query_vector": vector}
    except Exception:
        logger.warning("Embedding failed — retrieval will be skipped", exc_info=True)
        return {"query_vector": []}


def search_node(state: RAGState) -> dict:
    vector = state.get("query_vector", [])
    if not vector:
        return {"candidates": []}
    try:
        candidates = search_chunks(vector)
        logger.info("Vector search returned %d candidates", len(candidates))
        return {"candidates": candidates}
    except Exception:
        logger.warning("Vector search failed", exc_info=True)
        return {"candidates": []}


def rerank_node(state: RAGState) -> dict:
    candidates = state.get("candidates", [])
    if not candidates:
        return {"chunks": []}
    query = state.get("rewritten_query", state["user_query"])
    try:
        reranked = rerank_chunks(query, candidates)
        logger.info("Reranker selected %d chunks", len(reranked))
        return {"chunks": reranked}
    except Exception:
        logger.warning(
            "Reranker unavailable — falling back to vector scores", exc_info=True
        )
        fallback = sorted(candidates, key=lambda c: c.score, reverse=True)[
            : settings.RERANK_TOP_N
        ]
        return {"chunks": fallback}


def generate_node(state: RAGState) -> dict:
    chunks = state.get("chunks", [])
    chat_history = state.get("chat_history", [])
    user_query = state["user_query"]
    answer, context = generate_answer(user_query, chunks, chat_history)
    logger.info("Generated answer (%d chars) with %d chunks", len(answer), len(chunks))
    return {"context": context, "answer": answer}


def guardian_node(state: RAGState) -> dict:
    answer = state["answer"]
    context = state.get("context", "")
    is_grounded, final_answer = check_groundedness(answer, context)
    if not is_grounded:
        logger.warning("Response flagged as not fully grounded")
    return {"is_grounded": is_grounded, "final_answer": final_answer}


# ── Graph construction ────────────────────────────────────────


def build_rag_graph():
    """Construct and compile the RAG state graph.

    Topology (linear for the PoC):

        rewrite → embed → search → rerank → generate → guardian → END

    To add conditional routing later (e.g. re-generate when
    Guardian flags the answer), replace the generate→guardian
    edge with add_conditional_edges().
    """
    graph = StateGraph(RAGState)

    graph.add_node("rewrite", rewrite_node)
    graph.add_node("embed", embed_node)
    graph.add_node("search", search_node)
    graph.add_node("rerank", rerank_node)
    graph.add_node("generate", generate_node)
    graph.add_node("guardian", guardian_node)

    graph.set_entry_point("rewrite")
    graph.add_edge("rewrite", "embed")
    graph.add_edge("embed", "search")
    graph.add_edge("search", "rerank")
    graph.add_edge("rerank", "generate")
    graph.add_edge("generate", "guardian")
    graph.add_edge("guardian", END)

    return graph.compile()


rag_pipeline = build_rag_graph()
