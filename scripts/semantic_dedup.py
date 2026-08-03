#!/usr/bin/env python3
"""
semantic_dedup.py
Scanner mensal de near-duplicates no knowledge_base via Qdrant HNSW index.
Rodo no primeiro domingo de cada mês (cron: 0 3 1 * *).

Regras:
- Ignora coleções com prefixo em DEDUP_EXEMPT_PREFIXES (csv)
- Não deleta automaticamente — apenas emite relatório JSON de candidatos
- Threshold de similaridade: 0.92 (configurável)
- Usa query_batch_points() para delegar busca de vizinhos ao Qdrant
  (HNSW index nativo, O(n/batch) requests em vez de O(n²) brute-force)

Uso:
  python3 semantic_dedup.py [--collection knowledge_base] [--threshold 0.92] [--dry-run]
"""

import os
import sys
import json
import math
import argparse
import requests
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Dict, Optional

try:
    from qdrant_client import QdrantClient, models
    _has_qdrant_client = True
except ImportError:
    _has_qdrant_client = False

# ─── Config (profile-aware) ────────────────────────────────────────────────
from hermes_env import hermes_home, logs_dir

QDRANT_URL = os.environ.get("QDRANT_URL", "http://localhost:6333")
COLLECTION = os.environ.get("QDRANT_COLLECTION", "knowledge_base")
SCROLL_LIMIT = 50  # paginação Qdrant (evita timeout em coleções grandes)
SIMILARITY_THRESHOLD = 0.92
TOP_NEIGHBORS = 10

LOG_DIR = logs_dir()
LOG_FILE = LOG_DIR / "semantic_dedup.log"
REPORT_FILE = LOG_DIR / "semantic_dedup_report.json"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def log_message(msg: str):
    ts = now_iso()
    line = f"[{ts}] {msg}"
    print(line)
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


# ─── Qdrant Operations ────────────────────────────────────────────────────

def scroll_all_chunks(collection: str) -> List[Dict]:
    """
    Carrega todos os pontos da coleção paginando via scroll.
    Retorna lista de {id, vector, payload}.
    """
    all_chunks = []
    offset = None
    scanned = 0

    while True:
        payload = {
            "limit": SCROLL_LIMIT,
            "with_payload": True,
            "with_vector": True,
        }
        if offset is not None:
            payload["offset"] = offset

        try:
            resp = requests.post(
                f"{QDRANT_URL}/collections/{collection}/points/scroll",
                headers={"Content-Type": "application/json"},
                json=payload,
                timeout=30,
            )
            resp.raise_for_status()
            data = resp.json()
            result = data.get("result", {})
            points = result.get("points", [])

            if not points:
                break

            for point in points:
                # Pegar apenas vetor dense para similarity
                vector = point.get("vector")
                dense = None
                if isinstance(vector, dict):
                    dense = vector.get("dense")
                elif isinstance(vector, list):
                    dense = vector  # fallback: vetor simples

                if dense:
                    all_chunks.append({
                        "id": point.get("id"),
                        "vector": dense,
                        "payload": point.get("payload", {}),
                    })

            scanned += len(points)
            offset = result.get("next_page_offset")
            if offset is None:
                break

        except Exception as e:
            log_message(f"❌ Erro no scroll Qdrant: {e}")
            break

    log_message(f"📊 Total chunks carregados: {len(all_chunks)} / {scanned} escaneados")
    return all_chunks


def cosine_similarity(v1: List[float], v2: List[float]) -> float:
    """Calcula cosine similarity entre dois vetores."""
    if len(v1) != len(v2):
        return 0.0

    dot = sum(a * b for a, b in zip(v1, v2))
    norm1 = math.sqrt(sum(a * a for a in v1))
    norm2 = math.sqrt(sum(b * b for b in v2))

    if norm1 == 0 or norm2 == 0:
        return 0.0

    return dot / (norm1 * norm2)


def find_near_duplicates(chunks: List[Dict],
                         threshold: float = SIMILARITY_THRESHOLD,
                         collection: str = COLLECTION) -> List[Dict]:
    """
    Encontra pares de near-duplicates via Qdrant query_batch_points.
    Delega a busca de vizinhos ao índice HNSW nativo do Qdrant —
    O(n/batch) HTTP requests em vez de O(n²) brute-force local.

    Cada ponto consulta o Qdrant pelo vizinho mais próximo acima de
    threshold, excluindo self-match via HasIdCondition no servidor.
    """
    n = len(chunks)
    if n < 2:
        return []

    if not _has_qdrant_client:
        log_message("⚠️ qdrant-client não instalado. Instale com: pip install qdrant-client")
        log_message("   Fallback não disponível — pulando dedup.")
        return []

    client = QdrantClient(url=QDRANT_URL)
    BATCH_SIZE = max(1, min(200, n // 10 + 1))  # escala com tamanho da coleção
    candidates = []
    ids_seen = set()

    for batch_start in range(0, n, BATCH_SIZE):
        batch = chunks[batch_start:batch_start + BATCH_SIZE]

        requests_list = []
        for p in batch:
            point_id = p["id"]
            vec = p.get("vector")
            if vec is None:
                continue
            requests_list.append(
                models.QueryRequest(
                    query=vec,
                    using="dense",
                    limit=1,
                    score_threshold=threshold,
                    filter=models.Filter(
                        must_not=[
                            models.HasIdCondition(has_id=[point_id])
                        ]
                    ),
                    with_payload=True,
                    with_vector=False,
                )
            )

        if not requests_list:
            continue

        try:
            results = client.query_batch_points(
                collection_name=collection,
                requests=requests_list,
            )
        except Exception as e:
            log_message(f"❌ Erro query_batch_points (batch {batch_start}): {e}")
            continue

        # results is list[QueryResponse]; each has .points → list[ScoredPoint]
        for p, query_resp in zip(batch, results):
            scored_points = query_resp.points if hasattr(query_resp, 'points') else []
            if not scored_points:
                continue
            best = scored_points[0]
            pair_key = tuple(sorted([str(p["id"]), str(best.id)]))
            if pair_key in ids_seen:
                continue
            ids_seen.add(pair_key)
            candidates.append({
                "chunk_id_a": str(p["id"]),
                "chunk_id_b": str(best.id),
                "similarity": round(best.score, 6),
                "source_a": p["payload"].get("source", "unknown"),
                "source_b": (best.payload or {}).get("source", "unknown"),
                "title_a": p["payload"].get("title", "")[:60],
                "title_b": (best.payload or {}).get("title", "")[:60],
                "text_preview_a": p["payload"].get("text", "")[:100],
                "text_preview_b": (best.payload or {}).get("text", "")[:100],
            })

    # Ordenar por similaridade decrescente
    candidates.sort(key=lambda x: x["similarity"], reverse=True)
    return candidates


def generate_report(candidates: List[Dict], collection: str, threshold: float, scanned: int) -> Dict:
    """Gera relatório estruturado em JSON."""
    return {
        "timestamp": now_iso(),
        "collection": collection,
        "threshold": threshold,
        "scanned_chunks": scanned,
        "near_duplicate_pairs": len(candidates),
        "candidates": candidates,
        "recommendation": (
            f"{len(candidates)} pares de near-duplicates encontrados. "
            "Revisar manualmente e aplicar merge via Qdrant point update se aprovado."
        ),
    }


def main():
    parser = argparse.ArgumentParser(description="Semantic Dedup Scanner")
    parser.add_argument("--collection", default=COLLECTION, help="Nome da coleção Qdrant")
    parser.add_argument("--threshold", type=float, default=SIMILARITY_THRESHOLD, help="Threshold cosine similarity")
    parser.add_argument("--dry-run", action="store_true", help="Só escaneia, não salva relatório")
    args = parser.parse_args()

    collection = args.collection

    # Ignorar coleções com prefixos exempt (via DEDUP_EXEMPT_PREFIXES env var)
    exempt_prefixes = os.environ.get("DEDUP_EXEMPT_PREFIXES", "").split(",")
    exempt_prefixes = [p.strip() for p in exempt_prefixes if p.strip()]
    for prefix in exempt_prefixes:
        if collection.startswith(prefix):
            log_message(f"⏭️ Coleção '{collection}' é exempt (prefixo '{prefix}'). Saindo.")
            return

    log_message(f"🚀 Iniciando semantic dedup (collection={collection}, threshold={args.threshold}, dry_run={args.dry_run})")

    # Carregar chunks
    chunks = scroll_all_chunks(collection)

    if not chunks:
        log_message("⚠️ Nenhum chunk encontrado na coleção.")
        return

    # Encontrar near-duplicates
    log_message(f"🔍 Analisando similaridade entre {len(chunks)} chunks...")
    candidates = find_near_duplicates(chunks, threshold=args.threshold)

    # Gerar relatório
    report = generate_report(candidates, collection, args.threshold, len(chunks))

    log_message("=" * 60)
    log_message("📊 RELATÓRIO SEMANTIC DEDUP")
    log_message("=" * 60)
    log_message(f"  Chunks escaneados:      {report['scanned_chunks']}")
    log_message(f"  Near-duplicate pairs:   {report['near_duplicate_pairs']}")

    if candidates:
        log_message(f"  Top similaridade:       {candidates[0]['similarity']:.4f}")
        log_message(f"  Top par:                {candidates[0]['chunk_id_a']} ↔ {candidates[0]['chunk_id_b']}")
    else:
        log_message("  Nenhum near-duplicate encontrado.")

    log_message("=" * 60)

    # Salvar relatório JSON
    if not args.dry_run and candidates:
        try:
            REPORT_FILE.parent.mkdir(parents=True, exist_ok=True)
            with open(REPORT_FILE, "w", encoding="utf-8") as f:
                json.dump(report, f, ensure_ascii=False, indent=2)
            log_message(f"📄 Relatório salvo: {REPORT_FILE}")
        except Exception as e:
            log_message(f"❌ Erro ao salvar relatório: {e}")

    # Output JSON para stderr (parseável)
    print(json.dumps(report, ensure_ascii=False, indent=2), file=sys.stderr)

    log_message("✅ Semantic dedup completo.")


if __name__ == "__main__":
    main()
