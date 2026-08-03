#!/usr/bin/env python3
"""
decay_scanner.py
Script de arquivamento seletivo de chunks IA-generated com baixa importância.
Roda via cron semanal (0 3 * * 0).

Regras:
- source_type in ["human", "procedural"] → exempt (nunca arquiva)
- importance_score >= 0.7 → exempt
- archived == True → skip (já arquivado)
- half_life: 90d se importance_score >= 0.3, senão 30d
- decay_score < 0.1:
  - Se confidence_score >= 0.7 → alerta (reporta, não arquiva)
  - Senão → archive (archived = True)
- Coleções com prefixo em DECAY_EXEMPT_PREFIXES (csv) são ignoradas

Uso:
  python3 decay_scanner.py [--collection knowledge_base_hybrid] [--dry-run]
"""

import os
import sys
import json
import math
import argparse
import requests
from datetime import datetime, timezone
from pathlib import Path

# ─── Config (profile-aware) ────────────────────────────────────────────────
from hermes_env import hermes_home, logs_dir

QDRANT_URL = os.environ.get("QDRANT_URL", "http://localhost:6333")
COLLECTION = os.environ.get("QDRANT_COLLECTION", "knowledge_base")
SCROLL_LIMIT = 100  # paginação Qdrant
LOG_DIR = logs_dir()
LOG_FILE = LOG_DIR / "decay_scanner.log"

# ─── Helpers ──────────────────────────────────────────────────────────────

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def calculate_decay_score(last_accessed_at: str, importance_score: float) -> float:
    """
    Calcula decay exponencial: score = exp(-ln(2) * age_days / half_life).
    Chunks mais importantes persistem mais (half-lives maiores).
    """
    try:
        last = datetime.fromisoformat(last_accessed_at.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        # Se timestamp inválido, assumir now (não decaiu ainda)
        return 1.0
    
    now = datetime.now(timezone.utc)
    age_days = max(0, (now - last).total_seconds() / 86400)
    
    # Correção: half-life MAIOR para chunks mais importantes
    if importance_score >= 0.3:
        half_life = 90  # chunks médio/alto → 90 dias
    else:
        half_life = 30  # chunks baixo → 30 dias
    
    decay_score = math.exp(-math.log(2) * age_days / half_life)
    return decay_score


def ensure_log_dir():
    """Cria diretório de logs se não existir."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)


def log_message(msg: str):
    """Loga para stdout e append no arquivo."""
    ts = now_iso()
    line = f"[{ts}] {msg}"
    print(line)
    ensure_log_dir()
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")


# ─── Qdrant Operations ────────────────────────────────────────────────────

def scroll_chunks(collection: str, limit: int = SCROLL_LIMIT):
    """
    Generator que itera sobre todos os pontos da coleção via scroll.
    Evita carregar a coleção inteira em memória.
    """
    offset = None
    total_scanned = 0
    
    while True:
        payload = {
            "limit": limit,
            "with_payload": True,
            "with_vector": False,
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
                yield point
                total_scanned += 1
            
            offset = result.get("next_page_offset")
            if offset is None:
                break
                
        except Exception as e:
            log_message(f"❌ Erro no scroll Qdrant: {e}")
            break
    
    log_message(f"📊 Total de chunks escaneados: {total_scanned}")


def update_point_archived(point_id: str, collection: str, decay_score: float, dry_run: bool = False):
    """Atualiza payload do ponto: archived=True + decay_score calculado."""
    if dry_run:
        log_message(f"  [DRY-RUN] Arquivaria ponto {point_id} (decay_score={decay_score:.4f})")
        return True
    
    try:
        resp = requests.post(
            f"{QDRANT_URL}/collections/{collection}/points/payload",
            headers={"Content-Type": "application/json"},
            json={
                "points": [point_id],
                "payload": {
                    "archived": True,
                    "decay_score": decay_score,
                },
            },
            timeout=10,
        )
        resp.raise_for_status()
        return True
    except Exception as e:
        log_message(f"  ❌ Falha ao arquivar ponto {point_id}: {e}")
        return False


# ─── Main ─────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Decay Scanner — Arquivamento seletivo de chunks")
    parser.add_argument("--collection", default=COLLECTION, help="Nome da coleção Qdrant")
    parser.add_argument("--dry-run", action="store_true", help="Simulação — não modifica nada")
    parser.add_argument("--threshold", type=float, default=0.1, help="Threshold de decay para arquivamento")
    args = parser.parse_args()
    
    collection = args.collection
    
    # Ignorar coleções com prefixos exempt (via DECAY_EXEMPT_PREFIXES env var)
    exempt_prefixes = os.environ.get("DECAY_EXEMPT_PREFIXES", "").split(",")
    exempt_prefixes = [p.strip() for p in exempt_prefixes if p.strip()]
    for prefix in exempt_prefixes:
        if collection.startswith(prefix):
            log_message(f"⏭️ Coleção '{collection}' é exempt (prefixo '{prefix}'). Saindo.")
            return
    
    log_message(f"🚀 Iniciando decay scanner (collection={collection}, threshold={args.threshold}, dry_run={args.dry_run})")
    
    # Métricas
    stats = {
        "scanned": 0,
        "archived": 0,
        "alerted": 0,
        "skipped_human": 0,
        "skipped_procedural": 0,
        "skipped_high_importance": 0,
        "skipped_already_archived": 0,
        "failed": 0,
    }
    
    alerts = []  # Lista de alertas (decay < threshold mas confidence >= 0.7)
    
    for point in scroll_chunks(collection):
        stats["scanned"] += 1
        
        point_id = point.get("id")
        payload = point.get("payload", {})
        
        source_type = payload.get("source_type", "unknown")
        importance_score = payload.get("importance_score", 0.5)
        archived = payload.get("archived", False)
        last_accessed_at = payload.get("last_accessed_at", payload.get("created_at", now_iso()))
        confidence_score = payload.get("confidence_score", 1.0)
        
        # Skip: já arquivado
        if archived:
            stats["skipped_already_archived"] += 1
            continue
        
        # Skip: humano (exempt)
        if source_type == "human":
            stats["skipped_human"] += 1
            continue
        
        # Skip: procedural (exempt)
        if source_type == "procedural":
            stats["skipped_procedural"] += 1
            continue
        
        # Skip: alta importância
        if importance_score >= 0.7:
            stats["skipped_high_importance"] += 1
            continue
        
        # Calcular decay
        decay_score = calculate_decay_score(last_accessed_at, importance_score)
        
        # Verificar threshold
        if decay_score < args.threshold:
            # Regra decay-confidence: se confidence alto, alerta em vez de arquivar
            if confidence_score >= 0.7:
                stats["alerted"] += 1
                alerts.append({
                    "point_id": point_id,
                    "decay_score": round(decay_score, 4),
                    "confidence_score": round(confidence_score, 2),
                    "importance_score": round(importance_score, 2),
                    "age_days": round((datetime.now(timezone.utc) - datetime.fromisoformat(last_accessed_at.replace("Z", "+00:00"))).total_seconds() / 86400, 1),
                    "reason": "decay < threshold mas confidence >= 0.7 — revisão manual recomendada",
                })
                log_message(f"  ⚠️ ALERTA: ponto {point_id} (decay={decay_score:.4f}, confidence={confidence_score:.2f}) — revisão manual recomendada")
            else:
                # Arquivar
                ok = update_point_archived(point_id, collection, decay_score, args.dry_run)
                if ok:
                    stats["archived"] += 1
                    log_message(f"  📦 Arquivado: ponto {point_id} (decay={decay_score:.4f}, importance={importance_score:.2f})")
                else:
                    stats["failed"] += 1
    
    # Relatório JSON estruturado
    report = {
        "timestamp": now_iso(),
        "collection": collection,
        "threshold": args.threshold,
        "dry_run": args.dry_run,
        "scanned": stats["scanned"],
        "archived": stats["archived"],
        "alerted": stats["alerted"],
        "skipped_human": stats["skipped_human"],
        "skipped_procedural": stats["skipped_procedural"],
        "skipped_high_importance": stats["skipped_high_importance"],
        "skipped_already_archived": stats["skipped_already_archived"],
        "failed": stats["failed"],
        "alerts": alerts,
    }
    
    log_message("=" * 60)
    log_message("📊 RELATÓRIO DECAY SCANNER")
    log_message("=" * 60)
    log_message(f"  Escanados:              {stats['scanned']}")
    log_message(f"  Arquivados:             {stats['archived']}")
    log_message(f"  Alertas (decay+conf.):  {stats['alerted']}")
    log_message(f"  Skipped human:          {stats['skipped_human']}")
    log_message(f"  Skipped procedural:     {stats['skipped_procedural']}")
    log_message(f"  Skipped high imp.:      {stats['skipped_high_importance']}")
    log_message(f"  Skipped archived:       {stats['skipped_already_archived']}")
    log_message(f"  Falhas:                 {stats['failed']}")
    log_message("=" * 60)
    
    # JSON report to stderr (parseável)
    print(json.dumps(report, ensure_ascii=False, indent=2), file=sys.stderr)
    
    log_message("✅ Decay scanner completo.")


if __name__ == "__main__":
    main()
