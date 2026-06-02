"""
Embedding client. Provider-agnostic — defaults to OpenRouter, configurable
via EMBEDDING_API_BASE and EMBEDDING_MODEL for local Ollama/vLLM/llama.cpp.
Mandatory dimension validation.
"""
import os
import logging

import httpx

logger = logging.getLogger("cognitive-worker.embedding")

OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")
EMBEDDING_DIMS = int(os.environ.get("EMBEDDING_DIMS", "4096"))
EMBEDDING_API_BASE = os.environ.get(
    "EMBEDDING_API_BASE", "https://openrouter.ai/api/v1"
)
EMBEDDING_MODEL = os.environ.get(
    "EMBEDDING_MODEL", "qwen/qwen3-embedding-8b"
)


async def get_embedding(text: str) -> list[float]:
    """
    Generates embedding via the configured backend.
    Validates that the returned dimensions match EMBEDDING_DIMS.
    """
    headers = {"Content-Type": "application/json"}

    if "openrouter" in EMBEDDING_API_BASE.lower():
        if not OPENROUTER_API_KEY:
            raise RuntimeError("OPENROUTER_API_KEY is required for OpenRouter")
        headers["Authorization"] = f"Bearer {OPENROUTER_API_KEY}"
        headers["HTTP-Referer"] = "https://localhost"
        headers["X-Title"] = "Cognitive-Agent-MaaS"

    payload = {
        "model": EMBEDDING_MODEL,
        "input": text,
        "dimensions": EMBEDDING_DIMS,  # OpenAI/OpenRouter-specific; ignored by Ollama/vLLM
    }

    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(
            f"{EMBEDDING_API_BASE}/embeddings",
            headers=headers,
            json=payload,
        )
        resp.raise_for_status()
        data = resp.json()

    vec = data["data"][0]["embedding"]

    # ─── Critical dimension validation ──────────────────────────────────────
    if len(vec) != EMBEDDING_DIMS:
        raise ValueError(
            f"Embedding dimension mismatch: "
            f"expected {EMBEDDING_DIMS}, got {len(vec)}. "
            f"Check EMBEDDING_DIMS in .env and the Qdrant collection."
        )

    logger.debug(f"Embedding generated: {len(vec)} dims")
    return vec
