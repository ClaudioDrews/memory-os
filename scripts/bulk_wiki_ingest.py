#!/usr/bin/env python3
"""
Bulk ingest script — popula a knowledge_base Qdrant com todo conteúdo da wiki.
Fase A: one-shot dos arquivos existentes.
"""
import os
import re
import sys
import json
import time
import uuid
from pathlib import Path
from datetime import datetime, timezone
from collections import Counter

import aiohttp
import asyncio

# Sparse embedding (BM25) — optional, falls back to dense-only
try:
    from fastembed import SparseTextEmbedding
    _sparse_model = SparseTextEmbedding(model_name="Qdrant/bm25")
    _has_sparse = True
except ImportError:
    _sparse_model = None
    _has_sparse = False

# ─── Config ────────────────────────────────────────────────────────────────
OPENROUTER_KEY = os.environ.get("OPENROUTER_API_KEY")
QDRANT_URL = "http://localhost:6333"
COLLECTION = "knowledge_base"
WIKI_ROOT = Path(os.environ.get("WIKI_ROOT", str(Path.home() / "vault" / "wiki")))
EMBEDDING_MODEL = os.environ.get("EMBEDDING_MODEL", "qwen/qwen3-embedding-8b")
EMBEDDING_DIMS = int(os.environ.get("EMBEDDING_DIMS", "4096"))
# Same backend settings as the worker (docker/worker/services/embedding.py).
EMBEDDING_API_BASE = os.environ.get("EMBEDDING_API_BASE", "https://openrouter.ai/api/v1").rstrip("/")
EMBEDDING_API_KEY = os.environ.get("EMBEDDING_API_KEY") or OPENROUTER_KEY
MAX_TEXT_LEN = 8000      # truncate text for embedding (model context limit)
BATCH_SIZE = 8           # parallel embedding requests
RATE_LIMIT_SLEEP = 0.5   # seconds between batches

if not EMBEDDING_API_KEY:
    print("❌ EMBEDDING_API_KEY / OPENROUTER_API_KEY não encontrada no ambiente")
    sys.exit(1)

print(f"📁 Wiki root: {WIKI_ROOT}")
print(f"🎯 Coleção: {COLLECTION}")
print(f"🔑 Embeddings: {EMBEDDING_API_BASE} ({EMBEDDING_MODEL}, {EMBEDDING_DIMS}d)")

# ─── Encontrar todos os .md ────────────────────────────────────────────────
md_files = sorted(WIKI_ROOT.rglob("*.md"))
print(f"📄 Arquivos .md encontrados: {len(md_files)}")

# ─── Helpers ──────────────────────────────────────────────────────────────
def parse_frontmatter(text: str) -> tuple[dict, str]:
    """Extrai YAML frontmatter e retorna (metadata, body)."""
    if text.startswith("---"):
        parts = text.split("---", 2)
        if len(parts) >= 3:
            try:
                import yaml
                meta = yaml.safe_load(parts[1])
                body = parts[2].strip()
                return (meta if isinstance(meta, dict) else {}), body
            except Exception:
                pass
    return {}, text

def get_source_tag(path: Path) -> str:
    """Deriva source tag do path relativo à wiki."""
    rel = path.relative_to(WIKI_ROOT)
    parts = rel.parts
    if len(parts) > 1:
        return f"wiki-{parts[0]}"
    return "wiki-root"

def get_tags_from_frontmatter(meta: dict) -> list[str]:
    """Extrai tags do frontmatter."""
    tags = meta.get("tags", [])
    if isinstance(tags, str):
        tags = [t.strip() for t in tags.split(",")]
    return tags if isinstance(tags, list) else []


def get_sparse_vector(text: str) -> dict | None:
    """Gera sparse vector BM25 via fastembed (se disponível)."""
    if not _has_sparse:
        return None
    try:
        sparse_result = list(_sparse_model.embed([text]))[0]
        return {
            "indices": sparse_result.indices.tolist(),
            "values": sparse_result.values.tolist(),
        }
    except Exception as e:
        print(f"⚠️ Sparse embedding error: {e}")
        return None


async def get_embedding(session: aiohttp.ClientSession, text: str) -> list[float] | None:
    """Gera embedding denso via OpenRouter."""
    payload = {
        "model": EMBEDDING_MODEL,
        "input": text[:MAX_TEXT_LEN],
        "dimensions": EMBEDDING_DIMS,
    }
    try:
        async with session.post(
            f"{EMBEDDING_API_BASE}/embeddings",
            headers={
                "Authorization": f"Bearer {EMBEDDING_API_KEY}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=aiohttp.ClientTimeout(total=30),
        ) as resp:
            if resp.status != 200:
                body = await resp.text()
                print(f"⚠️ Embedding HTTP {resp.status}: {body[:200]}")
                return None
            data = await resp.json()
            return data["data"][0]["embedding"]
    except Exception as e:
        print(f"⚠️ Embedding error: {e}")
        return None

async def upsert_to_qdrant(session: aiohttp.ClientSession, points: list[dict]) -> bool:
    """Upsert batch de pontos no Qdrant."""
    try:
        async with session.put(
            f"{QDRANT_URL}/collections/{COLLECTION}/points",
            headers={"Content-Type": "application/json"},
            json={"points": points},
            timeout=aiohttp.ClientTimeout(total=30),
        ) as resp:
            if resp.status != 200:
                body = await resp.text()
                print(f"⚠️ Qdrant HTTP {resp.status}: {body[:200]}")
                return False
            return True
    except Exception as e:
        print(f"⚠️ Qdrant error: {e}")
        return False

# ─── Processamento principal ──────────────────────────────────────────────
async def main():
    stats = Counter({"ok": 0, "fail": 0, "skip": 0, "empty": 0})
    errors = []
    processed = 0
    total = len(md_files)

    connector = aiohttp.TCPConnector(limit=20)
    async with aiohttp.ClientSession(connector=connector) as session:
        # Verificar coleção — criar se não existir
        async with session.get(f"{QDRANT_URL}/collections/{COLLECTION}") as r:
            if r.status != 200:
                print(f"⚠️  Coleção {COLLECTION} não existe. Criando...")
                collection_config = {
                    "vectors": {
                        "dense": {
                            "size": EMBEDDING_DIMS,
                            "distance": "Cosine",
                        }
                    },
                    "sparse_vectors": {
                        "sparse": {},
                    },
                }
                async with session.put(
                    f"{QDRANT_URL}/collections/{COLLECTION}",
                    headers={"Content-Type": "application/json"},
                    json=collection_config,
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as cr:
                    if cr.status not in (200, 201):
                        body = await cr.text()
                        print(f"❌ Falha ao criar coleção ({cr.status}): {body[:200]}")
                        sys.exit(1)
                print(f"✅ Coleção {COLLECTION} criada (dense {EMBEDDING_DIMS}d + sparse BM25)")

        print("\n🚀 Iniciando ingestão em batches...\n")

        batch = []
        for idx, path in enumerate(md_files, 1):
            text = path.read_text(encoding="utf-8", errors="replace")
            if not text.strip():
                stats["empty"] += 1
                continue

            meta, body = parse_frontmatter(text)
            source = get_source_tag(path)
            tags = get_tags_from_frontmatter(meta)
            # Tag adicional da pasta
            folder_tag = source.replace("wiki-", "")
            if folder_tag not in tags:
                tags.append(folder_tag)

            # Título do frontmatter ou filename
            title = meta.get("title", path.stem)

            # Texto para embedding: título + body (sem frontmatter)
            embed_text = f"{title}\n\n{body}"[:MAX_TEXT_LEN]

            batch.append({
                "idx": idx,
                "path": str(path),
                "title": title,
                "source": source,
                "tags": tags,
                "embed_text": embed_text,
                "meta": meta,
            })

            if len(batch) >= BATCH_SIZE or idx == total:
                # Gerar embeddings em paralelo
                embed_tasks = [get_embedding(session, b["embed_text"]) for b in batch]
                vectors = await asyncio.gather(*embed_tasks)

                # Gerar sparse vectors (sync, fastembed é CPU-bound)
                sparse_vecs = [get_sparse_vector(b["embed_text"]) for b in batch]

                # Preparar pontos Qdrant
                points = []
                for b, vec, sparse in zip(batch, vectors, sparse_vecs):
                    if vec is None:
                        stats["fail"] += 1
                        errors.append(f"Embedding failed: {b['path']}")
                        continue

                    vector_payload = {"dense": vec}
                    if sparse is not None:
                        vector_payload["sparse"] = sparse

                    # Heurística de importance_score baseada no path/nome
                    importance_score = 0.5
                    path_str_lower = b["path"].lower()
                    if any(k in path_str_lower for k in ["architecture", "core", "important"]):
                        importance_score = 0.7
                    if any(t.lower() in ["important", "critical"] for t in b["tags"]):
                        importance_score = 0.8
                    if any(k in path_str_lower for k in ["draft", "temp", "old"]):
                        importance_score = 0.2
                    
                    now_iso = datetime.now(timezone.utc).isoformat()
                    
                    point = {
                        "id": str(uuid.uuid4()),
                        "vector": vector_payload,
                        "payload": {
                            "text": b["embed_text"],
                            "source": b["source"],
                            "tags": b["tags"],
                            "created_at": now_iso,
                            "reflection_count": 0,
                            "last_reflected": None,
                            "file_path": b["path"],
                            "title": b["title"],
                            "word_count": len(b["embed_text"].split()),
                            # ── Lineage fields (Fase 1)
                            "lineage_id": None,
                            "generation_model": None,
                            "generation_context_hash": None,
                            "retrieved_chunk_ids": None,
                            # ── Decay fields (Fase 2)
                            "decay_score": 1.0,
                            "last_accessed_at": now_iso,
                            "importance_score": importance_score,
                            "source_type": "human",
                            "confidence_score": 1.0,
                            "archived": False,
                        },
                    }
                    points.append(point)

                # Upsert
                if points:
                    ok = await upsert_to_qdrant(session, points)
                    if ok:
                        stats["ok"] += len(points)
                    else:
                        stats["fail"] += len(points)
                        for p in points:
                            errors.append(f"Qdrant upsert failed: {p['payload']['file_path']}")

                processed += len(batch)
                batch = []

                # Progresso
                pct = (processed / total) * 100
                print(f"  [{processed}/{total}] {pct:.1f}% | ✅ {stats['ok']} | ⚠️ {stats['fail']} | ⏭️ {stats['skip']} | 🈳 {stats['empty']}")

                # Rate limit breathing
                await asyncio.sleep(RATE_LIMIT_SLEEP)

    # ─── Relatório final ───────────────────────────────────────────────────
    print("\n" + "=" * 60)
    print("📊 RELATÓRIO DE INGESTÃO")
    print("=" * 60)
    print(f"  Total arquivos:     {total}")
    print(f"  Ingestados (ok):    {stats['ok']}")
    print(f"  Falhas:             {stats['fail']}")
    print(f"  Vazios:             {stats['empty']}")
    print(f"  Taxa de sucesso:    {(stats['ok']/max(total-stats['empty'],1)*100):.1f}%")
    print(f"\n  ⏱️  Finalizado: {datetime.now(timezone.utc).isoformat()}")

    if errors:
        print(f"\n  ⚠️  Primeiros erros ({min(10, len(errors))} de {len(errors)}):")
        for e in errors[:10]:
            print(f"     - {e}")

    # Verificar count final
    async with aiohttp.ClientSession() as s:
        async with s.get(f"{QDRANT_URL}/collections/{COLLECTION}") as r:
            data = await r.json()
            final_count = data.get("result", {}).get("points_count", "?")
            print(f"\n  📦 Pontos na coleção: {final_count}")

    print("\n✅ Bulk ingest completo.")
    return stats

if __name__ == "__main__":
    stats = asyncio.run(main())
    sys.exit(0 if stats["fail"] == 0 else 1)
