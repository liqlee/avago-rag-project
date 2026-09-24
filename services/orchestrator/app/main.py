import asyncio
import json
import logging
import queue
import threading
import time
import uuid

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse

from .config import settings
from .graph import rag_pipeline
from .models import (
    ChatCompletionRequest,
    ChatCompletionResponse,
    ChatMessage,
    Choice,
)
from .retriever import init_db

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)

app = FastAPI(title="RAG Orchestrator", version="2.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

_SENTINEL = object()

STEP_LABELS = {
    "rewrite": ("Rewriting query", lambda s: f'"{s.get("rewritten_query", "")[:100]}"'),
    "embed": ("Embedding query", None),
    "search": ("Searching knowledge base", lambda s: f'{len(s.get("candidates", []))} candidates found'),
    "rerank": ("Reranking results", lambda s: f'{len(s.get("chunks", []))} best chunks selected'),
    "generate": ("Generating answer", None),
    "guardian": ("Verifying groundedness", lambda s: "grounded" if s.get("is_grounded") else "flagged for review"),
}


@app.on_event("startup")
def startup():
    logger.info("Initializing database connection pool")
    init_db()
    logger.info("RAG Orchestrator ready (LangGraph pipeline)")


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/v1/models")
def list_models():
    return {
        "object": "list",
        "data": [
            {
                "id": settings.MODEL_NAME,
                "object": "model",
                "created": int(time.time()),
                "owned_by": "rag-orchestrator",
            }
        ],
    }


def _sse_chunk(comp_id: str, model: str, created: int, content: str, finish_reason=None):
    return "data: " + json.dumps({
        "id": comp_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": [{
            "index": 0,
            "delta": {"content": content} if content else {},
            "finish_reason": finish_reason,
        }],
    }) + "\n\n"


async def stream_pipeline(request: ChatCompletionRequest):
    user_messages = [m for m in request.messages if m.role == "user"]
    if not user_messages:
        raise HTTPException(status_code=400, detail="No user message found")

    user_query = user_messages[-1].content
    logger.info("Query (streaming): %s", user_query[:120])

    comp_id = f"chatcmpl-{uuid.uuid4().hex[:8]}"
    created = int(time.time())
    model = request.model

    yield "data: " + json.dumps({
        "id": comp_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}],
    }) + "\n\n"

    initial_state = {
        "user_query": user_query,
        "chat_history": [
            {"role": m.role, "content": m.content} for m in request.messages
        ],
        "model": model,
    }

    q: queue.Queue = queue.Queue()
    start = time.time()

    def run_in_thread():
        accumulated = {}
        try:
            for event in rag_pipeline.stream(initial_state, stream_mode="updates"):
                for node_name, node_output in event.items():
                    accumulated.update(node_output)
                    q.put(("step", node_name, dict(accumulated)))
            q.put(("done", dict(accumulated)))
        except Exception as e:
            q.put(("error", str(e)))

    thread = threading.Thread(target=run_in_thread, daemon=True)
    thread.start()

    final_state = {}

    while True:
        try:
            item = await asyncio.to_thread(q.get, timeout=300)
        except Exception:
            yield _sse_chunk(comp_id, model, created, "\n\n**Error:** Pipeline timed out.\n")
            break

        if item[0] == "error":
            yield _sse_chunk(comp_id, model, created, f"\n\n**Error:** {item[1]}\n")
            break

        if item[0] == "done":
            final_state = item[1]
            break

        _, node_name, accumulated_state = item
        final_state = accumulated_state
        label, detail_fn = STEP_LABELS.get(node_name, (node_name, None))
        elapsed_step = time.time() - start

        status_line = f"**{label}...**"
        if detail_fn:
            detail = detail_fn(accumulated_state)
            if detail:
                status_line += f" {detail}"
        status_line += f" ({elapsed_step:.1f}s)\n\n"

        yield _sse_chunk(comp_id, model, created, status_line)

    thread.join(timeout=5)

    elapsed = time.time() - start
    logger.info(
        "Pipeline done in %.1fs (chunks=%d, grounded=%s)",
        elapsed,
        len(final_state.get("chunks", [])),
        final_state.get("is_grounded", "N/A"),
    )

    final_answer = final_state.get("final_answer", final_state.get("answer", "No answer generated."))

    yield _sse_chunk(comp_id, model, created, "---\n\n")

    words = final_answer.split(" ")
    piece_size = 6
    for i in range(0, len(words), piece_size):
        piece = " ".join(words[i : i + piece_size])
        if i + piece_size < len(words):
            piece += " "
        yield _sse_chunk(comp_id, model, created, piece)
        await asyncio.sleep(0.01)

    yield _sse_chunk(comp_id, model, created, "", finish_reason="stop")
    yield "data: [DONE]\n\n"


def run_pipeline(request: ChatCompletionRequest) -> ChatCompletionResponse:
    start = time.time()

    user_messages = [m for m in request.messages if m.role == "user"]
    if not user_messages:
        raise HTTPException(status_code=400, detail="No user message found")

    user_query = user_messages[-1].content
    logger.info("Query: %s", user_query[:120])

    result = rag_pipeline.invoke({
        "user_query": user_query,
        "chat_history": [
            {"role": m.role, "content": m.content} for m in request.messages
        ],
        "model": request.model,
    })

    elapsed = time.time() - start
    logger.info(
        "Pipeline done in %.1fs (chunks=%d, grounded=%s)",
        elapsed,
        len(result.get("chunks", [])),
        result.get("is_grounded", "N/A"),
    )

    return ChatCompletionResponse(
        id=f"chatcmpl-{uuid.uuid4().hex[:8]}",
        model=request.model,
        choices=[
            Choice(
                message=ChatMessage(
                    role="assistant", content=result["final_answer"]
                ),
                finish_reason="stop",
            )
        ],
    )


@app.post("/v1/chat/completions")
async def chat_completions(request: ChatCompletionRequest):
    if request.stream:
        return StreamingResponse(
            stream_pipeline(request), media_type="text/event-stream"
        )
    result = await asyncio.to_thread(run_pipeline, request)
    return result
