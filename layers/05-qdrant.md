# Layer 5 — Vector Database (Qdrant)

> **Service:** Qdrant 1.17+ (Docker)
> **Collection:** `knowledge_base` (4096d Cosine + BM25 sparse)
> **Embedding:** Qwen3-Embedding-8B via OpenRouter (default; configurable)
> **Endpoint:** `http://localhost:6333`

## Why Qwen3-Embedding-8B

The default embedding model is **Qwen3-Embedding-8B** for four reasons:

1. **Multilingual** — strong performance across 50+ languages, including Portuguese, Spanish, and other non-English content common in real-world agent use
2. **Quality** — 4096-dimensional embeddings with high semantic fidelity; consistently ranks near the top of the MTEB leaderboard
3. **Speed** — fast inference at 8B parameters, suitable for hourly ingestion pipelines without bottlenecking the ARQ worker
4. **Cost** — affordable via OpenRouter ($0.025/1M tokens at time of writing); a full wiki re-index costs cents, not dollars

Users can switch to any OpenAI-compatible embedding API by setting `EMBEDDING_API_BASE` and `EMBEDDING_MODEL` in `.env`. If changing models, ensure `EMBEDDING_DIMS` matches both the new model's output and the Qdrant collection schema.

## What it stores

All knowledge that benefits from semantic search — wiki pages, session transcripts, raw documents, technical references. Content is ingested via the continuous ingest pipeline (hourly) and the wiki agent (scheduled).

## How the agent uses it

**Two access patterns:**

1. **Explicit** — `qdrant_search(query="memory architecture", top_k=3)` → agent calls the tool directly
2. **Automatic** — Icarus `pre_llm_call` injects relevant Qdrant results into the system prompt every turn (via `_search_qdrant()`)

**Search uses 4-level fallback cascade:**

```
1. Hybrid: dense (4096d cosine) + sparse (BM25) → RRF fusion
2. Dense-only: if sparse fails → pure vector search
3. Lexical: if Qdrant offline → markdown file search in vault
4. SQLite: if vault inaccessible → keyword search in lineage table
5. None: all exhausted → return empty (fail-open)
```

## Collection schema

```json
{
  "vectors": {
    "dense": { "size": 4096, "distance": "Cosine" }
  },
  "sparse_vectors": {
    "sparse": { "index": { "on_disk": false } }
  }
}
```

**Named vectors:** Points must use `{"dense": [...], "sparse": SparseVector(...)}`. Must match schema exactly — unnamed vectors are rejected silently.

## Decay and dedup

| Mechanism | Schedule | What it does |
|-----------|----------|--------------|
| **Decay scanner** | Weekly | Archives AI-generated points with low importance and high age. Exempts human-generated content and high-importance (>0.7) points. Formula: `decay_score = exp(-ln(2) * age_days / half_life)` |
| **Semantic dedup** | Monthly | Merges near-duplicate points (cosine > 0.92). Tags union, priority by source_type. |

## Context injection (Icarus enhancement)

```python
_search_qdrant(query, top_k=2, threshold=0.55):
    1. Embed user message via context_enhancer pipeline
    2. search_with_fallback() → 4-level cascade
    3. Results labeled [qdrant] in system prompt
    4. Per-session dedup by point ID
```

`context_enhancer.py` uses `EMBEDDING_API_BASE`, `EMBEDDING_MODEL` and the
optional `EMBEDDING_API_KEY` for OpenAI-compatible local or hosted providers.
The default OpenRouter endpoint continues to use `OPENROUTER_API_KEY`.

**Social closer gate:** Trivial messages ("ok", "thanks", emoji-only) skip Qdrant search entirely — no point embedding small talk.

## Embedding pipeline

```
File in $VAULT_PATH/wiki/
    │
    ▼
wiki-continuous-ingest (hourly cron)
    │ SHA-256 diff detection
    ▼
Redis queue (ARQ job)
    │
    ▼
ARQ Worker (Docker)
    │ embed via configured backend (default: Qwen3-Embedding-8B)
    │ get_sparse_embedding() → BM25 (fastembed, local)
    ▼
Qdrant upsert (with dedup check)
```

## Pitfalls

- **Named vectors are mandatory in Qdrant 1.17+:** `upsert` requires `"vector": {"dense": vec}`, plain vectors are rejected silently
- **`AsyncQdrantClient.search()` doesn't exist in qdrant-client 1.18:** Use REST API `POST /collections/{name}/points/search`
- **Embedding dimension mismatch is silent:** If env says 1024 but collection is 4096, OpenRouter truncates via Matryoshka — vectors are valid but degraded
- **Decay scanner is inert without payload metadata:** Requires `importance_score`, `last_accessed_at`, `confidence_score` in point payloads. Missing → all points get `decay_score=1.0` → nothing archived
- **Isolate persona/relational collections from decay/dedup** — they preserve temporal variation
