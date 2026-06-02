#!/usr/bin/env python3
"""
Context Enhancer — HYBRID search (semantic + BM25) on knowledge_base_hybrid
for prompt enrichment.

Runs as a synchronous function (fast, <1s) before each Hermes response.
If Qdrant is offline or embedding fails, returns "" (fail-open).

Usage:
  python3 context_enhancer.py "your query here"
  python3 context_enhancer.py --top-k 5 --threshold 0.50 "deploy docker"
  python3 context_enhancer.py --hybrid-off "your query here"   # forces dense-only
"""

import os
import sys
import json
import uuid
import hashlib
import sqlite3
import subprocess
import requests
import argparse
import re
import glob
from typing import List, Dict, Optional, Tuple
from pathlib import Path
import time
from datetime import datetime, timezone

# ─── Config ────────────────────────────────────────────────────────────────
OPENROUTER_KEY = os.environ.get("OPENROUTER_API_KEY")
if not OPENROUTER_KEY:
    env_path = Path.home() / ".env"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            if line.startswith("OPENROUTER_API_KEY="):
                OPENROUTER_KEY = line.split("=", 1)[1].strip().strip('"')
                break

QDRANT_URL = os.environ.get("QDRANT_URL", "http://localhost:6333")
COLLECTION = os.environ.get("QDRANT_COLLECTION", "knowledge_base")

EMBEDDING_MODEL = "qwen/qwen3-embedding-8b"
TOP_K_DEFAULT = 3
SCORE_THRESHOLD_DEFAULT = 0.55
MAX_TEXT_LEN = 8000
REQUEST_TIMEOUT = 10

# FastEmbed BM25 config
FASTEMBED_VENV = os.environ.get("FASTEMBED_VENV", "")

# Default: use current Python if venv not configured
_FASTEMBED_PYTHON = FASTEMBED_VENV if FASTEMBED_VENV else sys.executable
_FASTEMBED_SITEPKGS = os.environ.get(
    "FASTEMBED_SITEPKGS",
    os.path.join(os.path.dirname(sys.executable), "../lib/python3.12/site-packages")
)
BM25_MODEL = "Qdrant/bm25"

# Lineage config
LINEAGE_DB = os.environ.get(
    "STATE_DB_PATH",
    os.path.expanduser("~/.hermes/state.db")
)

# Telemetry config
TELEMETRY_LOG = os.environ.get(
    "TELEMETRY_LOG_PATH",
    os.path.expanduser("~/.hermes/logs/query-telemetry.jsonl")
)
TELEMETRY_MAX_BYTES = 10 * 1024 * 1024  # 10MB rotation

# ─── Lineage Registration ───────────────────────────────────────────────────

def register_lineage(
    session_id: str,
    query: str,
    retrieved_chunk_ids: List[str],
    generation_context_hash: str,
    generation_model: str = "unknown",
) -> Optional[str]:
    """
    Register generation provenance in the lineage DB.
    Fail-open: if it fails, log error and return None. Never breaks the critical path.
    """
    lineage_id = str(uuid.uuid4())
    try:
        with sqlite3.connect(LINEAGE_DB) as conn:
            conn.execute(
                """
                INSERT INTO lineage (lineage_id, session_id, query, retrieved_chunk_ids,
                                     generation_model, generation_context_hash, created_at)
                VALUES (?, ?, ?, ?, ?, ?, datetime('now'))
                """,
                (lineage_id, session_id, query,
                 json.dumps(retrieved_chunk_ids, ensure_ascii=False),
                 generation_model, generation_context_hash)
            )
            conn.commit()
        return lineage_id
    except Exception as e:
        print(f"[LINEAGE-WARNING] Failed to register lineage: {e}", file=sys.stderr)
        return None


# ─── Telemetry ─────────────────────────────────────────────────────────────

def _rotate_telemetry_if_needed() -> None:
    """Rotate telemetry file if >10MB. Rename to .1 and restart."""
    try:
        if os.path.exists(TELEMETRY_LOG) and os.path.getsize(TELEMETRY_LOG) > TELEMETRY_MAX_BYTES:
            rotated = TELEMETRY_LOG + ".1"
            if os.path.exists(rotated):
                os.remove(rotated)
            os.rename(TELEMETRY_LOG, rotated)
    except Exception as e:
        print(f"[TELEMETRY-WARNING] Rotation failed: {e}", file=sys.stderr)


def emit_telemetry(record: dict) -> None:
    """
    Append-only JSONL for query telemetry.
    Fail-open: never breaks the critical path.
    """
    try:
        os.makedirs(os.path.dirname(TELEMETRY_LOG), exist_ok=True)
        _rotate_telemetry_if_needed()
        with open(TELEMETRY_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception as e:
        print(f"[TELEMETRY-WARNING] Failed to write telemetry: {e}", file=sys.stderr)


def estimate_tokens(text: str) -> int:
    """Simple estimate: ~1.3 tokens per word (basic heuristic)."""
    return int(len(text.split()) * 1.3)


# ─── Core ───────────────────────────────────────────────────────────────────

def embed_query(text: str) -> Optional[List[float]]:
    """Generate dense embedding via OpenRouter qwen/qwen3-embedding-8b."""
    if not OPENROUTER_KEY:
        return None
    try:
        resp = requests.post(
            "https://openrouter.ai/api/v1/embeddings",
            headers={
                "Authorization": f"Bearer {OPENROUTER_KEY}",
                "Content-Type": "application/json"
            },
            json={
                "model": EMBEDDING_MODEL,
                "input": text[:MAX_TEXT_LEN]
            },
            timeout=REQUEST_TIMEOUT
        )
        resp.raise_for_status()
        return resp.json()["data"][0]["embedding"]
    except Exception as e:
        # Fail-open: log silently, return None
        print(f"[CE-ERROR] Embedding dense failed: {e}", file=sys.stderr)
        return None


def embed_query_sparse(text: str) -> Optional[Tuple[List[int], List[float]]]:
    """
    Generate sparse BM25 embedding via FastEmbed (subprocess in ai-lab venv).
    Query text passes via stdin — never embedded in a -c code string.
    Fail-open: if it fails, return None. Caller falls back to dense-only.
    """
    try:
        result = subprocess.run(
            [_FASTEMBED_PYTHON, "-c", """\
import os, sys, json
sys.path.insert(0, os.environ["FASTEMBED_SITEPKGS"])
from fastembed.sparse import SparseTextEmbedding
query = sys.stdin.read()
model = SparseTextEmbedding(model_name="Qdrant/bm25")
sparse = list(model.embed([query]))[0]
print(json.dumps({"indices": sparse.indices.tolist(), "values": sparse.values.tolist()}))
"""],
            input=text,
            capture_output=True, text=True, timeout=15
        )
        data = json.loads(result.stdout.strip())
        return data["indices"], data["values"]
    except Exception as e:
        print(f"[CE-ERROR] Embedding sparse failed: {e}", file=sys.stderr)
        return None


def search_knowledge_base(
    dense_vector: List[float],
    sparse_vector: Optional[Tuple[List[int], List[float]]] = None,
    top_k: int = TOP_K_DEFAULT,
    score_threshold: float = SCORE_THRESHOLD_DEFAULT
) -> List[Dict]:
    """
    Hybrid search in Qdrant: dense (semantic) + sparse (BM25 keyword)
    via prefetch + RRF. If sparse fails, fall back to dense-only.
    """
    try:
        if sparse_vector is not None:
            # Hybrid: prefetch dense + prefetch sparse → RRF
            resp = requests.post(
                f"{QDRANT_URL}/collections/{COLLECTION}/points/query",
                headers={"Content-Type": "application/json"},
                json={
                    "prefetch": [
                        {"query": dense_vector, "using": "dense", "limit": top_k * 3},
                        {"query": {"indices": sparse_vector[0], "values": sparse_vector[1]},
                         "using": "sparse", "limit": top_k * 3},
                    ],
                    "query": {"fusion": "rrf"},
                    "limit": top_k * 2,
                    "with_payload": True,
                },
                timeout=REQUEST_TIMEOUT + 5,
            )
        else:
            # Fallback: dense-only (collections with compatible named vectors)
            resp = requests.post(
                f"{QDRANT_URL}/collections/{COLLECTION}/points/query",
                headers={"Content-Type": "application/json"},
                json={
                    "query": dense_vector,
                    "using": "dense",
                    "limit": top_k,
                    "with_payload": True,
                },
                timeout=REQUEST_TIMEOUT,
            )
        resp.raise_for_status()
        data = resp.json()
        results = []
        raw_results = data.get("result", {})
        # API /points/search returns direct list; API /points/query returns {"points": [...]}
        points = raw_results if isinstance(raw_results, list) else raw_results.get("points", [])
        for r in points:
            score = r.get("score", 0)
            if score < score_threshold:
                continue
            payload = r.get("payload", {})
            results.append({
                "id": r.get("id", "unknown"),
                "score": score,
                "title": payload.get("title", "Untitled"),
                "content_preview": (payload.get("text", "") or "")[:400],
                "source": payload.get("source", "unknown"),
                "tags": payload.get("tags", [])
            })
        return results
    except Exception as e:
        print(f"[CE-ERROR] Qdrant search failed: {e}", file=sys.stderr)
        return []


# ─── Fallback: Lexical Search in Vault ──────────────────────────────────────

def tokenize_query(text: str) -> List[str]:
    """Tokenize query into relevant terms (lowercase, alphanumeric)."""
    # Remove basic Portuguese/English stopwords
    stopwords = {"o", "a", "os", "as", "um", "uma", "de", "da", "do", "em", "no", "na", "para", "com", "por", "que", "se", "e", "ou", "mas", "the", "a", "an", "is", "are", "was", "were", "be", "been", "have", "has", "had", "do", "does", "did", "will", "would", "could", "should"}
    tokens = re.findall(r'\b[a-zA-Z0-9]+\b', text.lower())
    return [t for t in tokens if t not in stopwords and len(t) > 2]


def lexical_search_in_vault(
    query_terms: List[str],
    top_k: int = TOP_K_DEFAULT,
    vault_root: str = os.environ.get("WIKI_PATH", os.path.expanduser("~/vault/wiki"))
) -> List[Dict]:
    """
    Lexical search in .md files under vault/wiki/.
    Rank by term match density / document size.
    Return top-k simulated chunks (or entire file if small).
    """
    if not query_terms:
        return []

    md_files = glob.glob(f"{vault_root}/**/*.md", recursive=True)
    if not md_files:
        return []

    scored = []
    for filepath in md_files:
        try:
            text = Path(filepath).read_text(encoding="utf-8", errors="replace").lower()
            if not text:
                continue

            matches = 0
            for term in query_terms:
                matches += text.count(term)

            if matches == 0:
                continue

            # Heuristic: matches / sqrt(word_count) — favors concise documents
            word_count = max(1, len(text.split()))
            density = matches / (word_count ** 0.5)  # sqrt(word_count) to not over-penalize medium texts
            scored.append({
                "filepath": filepath,
                "matches": matches,
                "word_count": word_count,
                "density": density,
                "title": Path(filepath).stem,
                "text": text[:2000],  # truncate for return
            })
        except Exception:
            continue

    if not scored:
        return []

    # Sort by descending density
    scored.sort(key=lambda x: x["density"], reverse=True)

    results = []
    for i, item in enumerate(scored[:top_k]):
        # Simulate a result chunk
        results.append({
            "id": f"lexical-{hashlib.md5(item['filepath'].encode()).hexdigest()[:16]}",
            "score": round(min(1.0, item["density"]), 2),
            "title": item["title"],
            "content_preview": item["text"][:400],
            "source": f"vault-{item['filepath'].replace(vault_root, '').lstrip('/')[:40]}",
            "tags": ["fallback", "lexical"],
            "fallback_level": "lexical",
        })

    if results:
        print(f"[CE-FALLBACK] Lexical search returned {len(results)} results from vault", file=sys.stderr)
    return results


# ─── Fallback: SQLite Keyword Search ────────────────────────────────────────

def sqlite_keyword_search(
    query_terms: List[str],
    top_k: int = TOP_K_DEFAULT
) -> List[Dict]:
    """
    Search terms in the lineage table (query field) and other state tables.
    Last resort — does not replace vault.
    """
    if not query_terms:
        return []

    try:
        with sqlite3.connect(LINEAGE_DB) as conn:
            conn.row_factory = sqlite3.Row
            c = conn.cursor()

            results = []
            # Search in lineage.query
            placeholders = " OR ".join(["query LIKE ?"] * len(query_terms))
            params = [f"%{term}%" for term in query_terms]
            c.execute(
                f"""
                SELECT lineage_id, session_id, query, generation_context_hash, created_at
                FROM lineage
                WHERE {placeholders}
                ORDER BY created_at DESC
                LIMIT ?
                """,
                params + [top_k * 2]
            )
            for row in c.fetchall():
                results.append({
                    "id": f"sqlite-{row['lineage_id'][:16]}",
                    "score": 0.5,
                    "title": f"Lineage {row['lineage_id'][:8]}...",
                    "content_preview": (row["query"] or "")[:400],
                    "source": f"sqlite-history-{row['session_id']}",
                    "tags": ["fallback", "sqlite"],
                    "fallback_level": "sqlite",
                })

            if results:
                print(f"[CE-FALLBACK] SQLite keyword search returned {len(results)} results", file=sys.stderr)
            return results[:top_k]
    except Exception as e:
        print(f"[CE-FALLBACK] SQLite search failed: {e}", file=sys.stderr)
        return []


# ─── Fallback Wrapper ──────────────────────────────────────────────────────

def search_with_fallback(
    dense_vector: Optional[List[float]] = None,
    sparse_vector: Optional[Tuple[List[int], List[float]]] = None,
    query_text: str = "",
    top_k: int = TOP_K_DEFAULT,
    score_threshold: float = SCORE_THRESHOLD_DEFAULT
) -> Tuple[List[Dict], str, float, float]:
    """
    4-level fallback cascade:
      1. Hybrid (dense + sparse + RRF) — normal mode
      2. Dense-only — if sparse fails or is None
      3. Lexical-only — if Qdrant goes down (ConnectionError, Timeout)
      4. SQLite keyword — if vault is inaccessible
    
    Returns (results, fallback_level, qdrant_latency_ms, fallback_latency_ms).
    """
    fallback_level = "hybrid"
    qdrant_latency_ms = 0.0
    t0 = time.perf_counter()

    # Level 1 or 2: Qdrant (hybrid if sparse available, otherwise dense-only)
    try:
        t_q0 = time.perf_counter()
        if sparse_vector is not None:
            resp = requests.post(
                f"{QDRANT_URL}/collections/{COLLECTION}/points/query",
                headers={"Content-Type": "application/json"},
                json={
                    "prefetch": [
                        {"query": dense_vector, "using": "dense", "limit": top_k * 3},
                        {"query": {"indices": sparse_vector[0], "values": sparse_vector[1]},
                         "using": "sparse", "limit": top_k * 3},
                    ],
                    "query": {"fusion": "rrf"},
                    "limit": top_k * 2,
                    "with_payload": True,
                },
                timeout=REQUEST_TIMEOUT + 5,
            )
            fallback_level = "hybrid"
        else:
            resp = requests.post(
                f"{QDRANT_URL}/collections/{COLLECTION}/points/query",
                headers={"Content-Type": "application/json"},
                json={
                    "query": dense_vector,
                    "using": "dense",
                    "limit": top_k,
                    "with_payload": True,
                },
                timeout=REQUEST_TIMEOUT,
            )
            fallback_level = "dense-only"

        resp.raise_for_status()
        qdrant_latency_ms = (time.perf_counter() - t_q0) * 1000
        data = resp.json()
        results = []
        raw_results = data.get("result", {})
        points = raw_results if isinstance(raw_results, list) else raw_results.get("points", [])
        for r in points:
            score = r.get("score", 0)
            if score < score_threshold:
                continue
            payload = r.get("payload", {})
            results.append({
                "id": r.get("id", "unknown"),
                "score": score,
                "title": payload.get("title", "Untitled"),
                "content_preview": (payload.get("text", "") or "")[:400],
                "source": payload.get("source", "unknown"),
                "tags": payload.get("tags", [])
            })

        if results:
            fallback_latency_ms = (time.perf_counter() - t0) * 1000
            return results, fallback_level, qdrant_latency_ms, fallback_latency_ms
    except requests.exceptions.ConnectionError:
        print("[CE-FALLBACK] Qdrant unavailable (ConnectionError), falling back to lexical search.", file=sys.stderr)
    except requests.exceptions.Timeout:
        print("[CE-FALLBACK] Qdrant timeout, falling back to lexical search.", file=sys.stderr)
    except Exception as e:
        # Unexpected Qdrant error (e.g. SparseIndexError, 5xx, etc.)
        # If sparse_vector existed, it might be a sparse error — try dense-only
        if sparse_vector is not None and "sparse" in str(e).lower():
            print(f"[CE-FALLBACK] Sparse index failed ('{e}'), trying dense-only...", file=sys.stderr)
            try:
                t_q2 = time.perf_counter()
                resp = requests.post(
                    f"{QDRANT_URL}/collections/{COLLECTION}/points/search",
                    headers={"Content-Type": "application/json"},
                    json={
                        "vector": dense_vector,
                        "using": "dense",
                        "limit": top_k,
                        "with_payload": True,
                    },
                    timeout=REQUEST_TIMEOUT,
                )
                resp.raise_for_status()
                qdrant_latency_ms = (time.perf_counter() - t_q2) * 1000
                data = resp.json()
                results = []
                raw_results = data.get("result", {})
                points = raw_results if isinstance(raw_results, list) else raw_results.get("points", [])
                for r in points:
                    score = r.get("score", 0)
                    if score < score_threshold:
                        continue
                    payload = r.get("payload", {})
                    results.append({
                        "id": r.get("id", "unknown"),
                        "score": score,
                        "title": payload.get("title", "Untitled"),
                        "content_preview": (payload.get("text", "") or "")[:400],
                        "source": payload.get("source", "unknown"),
                        "tags": payload.get("tags", [])
                    })
                if results:
                    fallback_latency_ms = (time.perf_counter() - t0) * 1000
                    print("[CE-FALLBACK] Dense-only worked after sparse error.", file=sys.stderr)
                    return results, "dense-only", qdrant_latency_ms, fallback_latency_ms
            except (requests.exceptions.ConnectionError, requests.exceptions.Timeout):
                print("[CE-FALLBACK] Qdrant also unavailable for dense-only — lexical fallback.", file=sys.stderr)
            except Exception as e2:
                print(f"[CE-FALLBACK] Dense-only also failed: {e2}", file=sys.stderr)
        else:
            print(f"[CE-FALLBACK] Qdrant general error ({e}), falling back to lexical.", file=sys.stderr)

    # Level 3: Lexical search in vault
    terms = tokenize_query(query_text)
    lexical_results = lexical_search_in_vault(terms, top_k=top_k)
    if lexical_results:
        fallback_latency_ms = (time.perf_counter() - t0) * 1000
        return lexical_results, "lexical", qdrant_latency_ms, fallback_latency_ms

    # Level 4: SQLite keyword search
    sqlite_results = sqlite_keyword_search(terms, top_k=top_k)
    if sqlite_results:
        fallback_latency_ms = (time.perf_counter() - t0) * 1000
        return sqlite_results, "sqlite", qdrant_latency_ms, fallback_latency_ms

    # Nothing worked
    fallback_latency_ms = (time.perf_counter() - t0) * 1000
    print("[CE-FALLBACK] All fallback levels exhausted.", file=sys.stderr)
    return [], "none", qdrant_latency_ms, fallback_latency_ms


def update_last_accessed_at(chunk_ids: list) -> None:
    """
    Update last_accessed_at on chunks returned by search.
    Reset decay for chunks that are actually being used.
    Fail-open: never breaks the critical query path.
    """
    if not chunk_ids:
        return
    try:
        now = datetime.now().astimezone().isoformat()
        requests.post(
            f"{QDRANT_URL}/collections/{COLLECTION}/points/payload",
            headers={"Content-Type": "application/json"},
            json={
                "points": chunk_ids,
                "payload": {"last_accessed_at": now},
            },
            timeout=5,
        )
    except Exception as e:
        print(f"[CE-WARNING] Failed to update last_accessed_at: {e}", file=sys.stderr)


def enhance_prompt(
    user_message: str,
    top_k: int = TOP_K_DEFAULT,
    score_threshold: float = SCORE_THRESHOLD_DEFAULT,
    format_mode: str = "markdown",
    hybrid: bool = True,
    session_id: Optional[str] = None,
    generation_model: str = "unknown",
) -> str:
    """
    Return a context block with relevant vault content (hybrid dense+BM25),
    or an empty string if nothing relevant found / services offline.
    Register lineage with chunk provenance and generated context hash.
    """
    # Resolve session_id
    if session_id is None:
        session_id = os.environ.get("HERMES_SESSION_ID", "standalone")

    # Skip very short or irrelevant queries
    if len(user_message.strip()) < 5:
        return ""

    # Very short or social query — skip
    social_keywords = {"hi", "hello", "hey", "yo", "sup", "ok", "thanks", "bye", "okay", "great"}
    if user_message.strip().lower() in social_keywords:
        return ""

    dense_vector = embed_query(user_message)
    sparse_vector = None
    if hybrid:
        sparse_vector = embed_query_sparse(user_message)

    # Fallback cascade: hybrid → dense-only → lexical → sqlite
    hits, fallback_level, qdrant_latency_ms, fallback_latency_ms = search_with_fallback(
        dense_vector=dense_vector,
        sparse_vector=sparse_vector,
        query_text=user_message,
        top_k=top_k,
        score_threshold=score_threshold
    )

    # If empty and we had no embedding, try pure lexical
    if not hits and dense_vector is None:
        terms = tokenize_query(user_message)
        lexical_results = lexical_search_in_vault(terms, top_k=top_k)
        if lexical_results:
            hits = lexical_results
            fallback_level = "lexical"

    if not hits:
        # Emit telemetry even when empty (records that search ran and returned nothing)
        emit_telemetry({
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "session_id": session_id,
            "query": user_message,
            "retrieval_mode": fallback_level,
            "retrieved_chunk_ids": [],
            "semantic_scores": [],
            "rerank_scores": [],
            "final_context_tokens": 0,
            "fallback_level": {"hybrid": 0, "dense-only": 1, "lexical": 2, "sqlite": 3, "none": -1}.get(fallback_level, -1),
            "llm_response_quality": None,
            "qdrant_latency_ms": round(qdrant_latency_ms, 2),
            "fallback_latency_ms": round(fallback_latency_ms, 2),
        })
        return ""

    # Update last_accessed_at for returned chunks (reset decay on real usage)
    retrieved_chunk_ids = [h["id"] for h in hits]
    update_last_accessed_at(retrieved_chunk_ids)

    if format_mode == "markdown":
        lines = ["\n## Relevant Context from Vault\n"]
        for i, h in enumerate(hits, 1):
            lines.append(f"### [{i}] {h['title']} (score: {h['score']:.2f})")
            lines.append(f"- **Source:** `{h['source']}` | Tags: {', '.join(str(t) for t in h['tags'])}")
            lines.append(f"- **Excerpt:** {h['content_preview']}...")
            lines.append("")
        context_str = "\n".join(lines)
    elif format_mode == "compact":
        parts = []
        for h in hits:
            parts.append(f"[{h['title'][:40]}] (s:{h['score']:.2f})")
        context_str = "\n".join(parts)
    else:
        context_str = json.dumps(hits, indent=2, ensure_ascii=False)

    generation_context_hash = hashlib.sha256(context_str.encode("utf-8")).hexdigest()[:32]

    register_lineage(
        session_id=session_id,
        query=user_message,
        retrieved_chunk_ids=retrieved_chunk_ids,
        generation_context_hash=generation_context_hash,
        generation_model=generation_model,
    )

    # Extract scores for telemetry
    semantic_scores = [h.get("score", 0) for h in hits]
    # For rerank_scores, use the same scores (internal Qdrant RRF, not exposed via simple API)
    rerank_scores = semantic_scores[:]

    final_context_tokens = estimate_tokens(context_str)

    emit_telemetry({
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "session_id": session_id,
        "query": user_message,
        "retrieval_mode": fallback_level,
        "retrieved_chunk_ids": retrieved_chunk_ids,
        "semantic_scores": semantic_scores,
        "rerank_scores": rerank_scores,
        "final_context_tokens": final_context_tokens,
        "fallback_level": {"hybrid": 0, "dense-only": 1, "lexical": 2, "sqlite": 3, "none": -1}.get(fallback_level, -1),
        "llm_response_quality": None,
        "qdrant_latency_ms": round(qdrant_latency_ms, 2),
        "fallback_latency_ms": round(fallback_latency_ms, 2),
    })

    return context_str


def main():
    parser = argparse.ArgumentParser(description="Context Enhancer — Hybrid search in knowledge_base")
    parser.add_argument("query", nargs="?", help="User message/query")
    parser.add_argument("--top-k", type=int, default=TOP_K_DEFAULT, help=f"Number of results (default: {TOP_K_DEFAULT})")
    parser.add_argument("--threshold", type=float, default=SCORE_THRESHOLD_DEFAULT, help=f"Minimum score (default: {SCORE_THRESHOLD_DEFAULT})")
    parser.add_argument("--format", choices=["markdown", "compact", "json"], default="markdown", help="Output format")
    parser.add_argument("--silent", action="store_true", help="Silent — return empty on error")
    parser.add_argument("--hybrid-off", action="store_true", help="Force dense-only (ignore BM25)")
    args = parser.parse_args()

    if not args.query:
        args.query = sys.stdin.read().strip() or "how ARQ worker works"

    try:
        result = enhance_prompt(
            args.query,
            top_k=args.top_k,
            score_threshold=args.threshold,
            format_mode=args.format,
            hybrid=not args.hybrid_off,
            generation_model=os.environ.get("HERMES_MODEL", "unknown"),
        )
        print(result)
    except Exception as e:
        if not args.silent:
            print(f"[CE-ERROR] {e}", file=sys.stderr)
        sys.exit(1)

if __name__ == "__main__":
    main()
