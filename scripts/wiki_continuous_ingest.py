#!/usr/bin/env python3
"""
wiki-continuous-ingest.py
Detecta novos/modificados .md no vault e enfileira no ARQ worker.
Roda no host, acessa Redis local (127.0.0.1:6379) e Qdrant (localhost:6333).
"""
import os
import sys
import json
import hashlib
import asyncio
from pathlib import Path
from datetime import datetime, timezone

from dotenv import load_dotenv
from arq import create_pool
from arq.connections import RedisSettings
import redis.asyncio as aioredis

# ─── Config (profile-aware) ────────────────────────────────────────────────
from hermes_env import hermes_home, wiki_state_file, wiki_failures_file

ENV_PATH = Path.home() / "memory-os" / "docker" / ".env"
if ENV_PATH.exists():
    load_dotenv(ENV_PATH)

WIKI_ROOT = Path(os.environ.get("WIKI_ROOT", str(Path.home() / "vault" / "wiki")))
STATE_FILE = wiki_state_file()
FAILURES_FILE = wiki_failures_file()
REDIS_PASSWORD = os.environ.get("REDIS_PASSWORD", "")

# Fallback: read REDIS_PASSWORD from Docker .env if not in environment (cron context)
if not REDIS_PASSWORD:
    DOCKER_ENV = Path.home() / "memory-os" / "docker" / ".env"
    if DOCKER_ENV.exists():
        load_dotenv(DOCKER_ENV)
        REDIS_PASSWORD = os.environ.get("REDIS_PASSWORD", "")

redis_settings = RedisSettings(
    host="127.0.0.1",
    port=6379,
    password=REDIS_PASSWORD or None,
)


def load_state() -> dict:
    if STATE_FILE.exists():
        with open(STATE_FILE) as f:
            return json.load(f)
    return {}


def save_state(state: dict):
    """Atomic write via tempfile + rename para evitar corrupção."""
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, STATE_FILE)


def file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16]


async def redis_ready() -> bool:
    """Verifica se Redis está acessível antes de enfileirar."""
    try:
        r = aioredis.Redis(
            host="127.0.0.1", port=6379,
            password=REDIS_PASSWORD or None,
            socket_connect_timeout=3,
            socket_timeout=3,
        )
        ok = await r.ping()
        await r.aclose()
        return bool(ok)
    except Exception as e:
        print(f"  ⚠️  Redis indisponível: {e}")
        return False


async def main():
    if not await redis_ready():
        print("❌ Redis não pronto. Docker stack pode estar subindo. Abortando.")
        return

    state = load_state()
    new_files = []
    modified_files = []
    skipped = 0
    total = 0

    # Varre todos os .md
    for path in sorted(WIKI_ROOT.rglob("*.md")):
        total += 1
        rel = str(path.relative_to(WIKI_ROOT))
        mtime = path.stat().st_mtime
        current_hash = file_hash(path)

        if rel not in state:
            new_files.append(rel)
            state[rel] = {"mtime": mtime, "hash": current_hash, "ingested_at": None}
        elif state[rel]["hash"] != current_hash:
            modified_files.append(rel)
            state[rel]["mtime"] = mtime
            state[rel]["hash"] = current_hash
            state[rel]["ingested_at"] = None
        else:
            skipped += 1

    files_to_ingest = new_files + modified_files

    if not files_to_ingest:
        print(f"⏭️  Nada novo. {total} arquivos rastreados, {skipped} inalterados.")
        return

    # Enfileirar no ARQ
    redis = await create_pool(redis_settings)
    enqueued = 0
    failed = 0
    failures = []

    for rel_path in files_to_ingest:
        abs_path = str(WIKI_ROOT / rel_path)
        try:
            job = await redis.enqueue_job(
                "process_wiki_file",
                file_path=f"/wiki/{rel_path}",  # path dentro do container
            )
            state[rel_path]["ingested_at"] = datetime.now(timezone.utc).isoformat()
            enqueued += 1
            print(f"  ✅ Enfileirado: {rel_path} (job: {job.job_id[:8]})")
        except Exception as e:
            failed += 1
            error_msg = str(e)
            # Classificar o erro para o DLQ
            error_lower = error_msg.lower()
            transient_patterns = ["timeout", "connection", "rate limit", "503", "502", "504",
                                  "unavailable", "too many requests", "refused", "reset"]
            permanent_patterns = ["400", "404", "not found", "invalid", "parse error",
                                  "deleted", "permission denied"]
            failure_class = "unknown"
            for p in transient_patterns:
                if p in error_lower:
                    failure_class = "transient"
                    break
            if failure_class == "unknown":
                for p in permanent_patterns:
                    if p in error_lower:
                        failure_class = "permanent"
                        break
            
            failures.append({
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "file": rel_path,
                "error": error_msg,
                "failure_class": failure_class,   # NOVO: classificação
                "reported": False,                 # NOVO: ainda não reportado
                "retry_count": 0,                  # NOVO: zero retries
            })
            print(f"  ⚠️  Falha: {rel_path} — {e} [{failure_class}]")

    await redis.aclose()
    save_state(state)

    # Persistir falhas para DLQ simples (atômico, últimas 500)
    if failures:
        FAILURES_FILE.parent.mkdir(parents=True, exist_ok=True)
        existing = []
        if FAILURES_FILE.exists():
            with open(FAILURES_FILE) as f:
                existing = json.load(f)
        existing.extend(failures)
        existing = existing[-500:]
        tmp = FAILURES_FILE.with_suffix(".tmp")
        with open(tmp, "w") as f:
            json.dump(existing, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, FAILURES_FILE)

    print(f"\n📊 {total} arquivos rastreados")
    print(f"   Novos: {len(new_files)} | Modificados: {len(modified_files)} | Inalterados: {skipped}")
    print(f"   Enfileirados: {enqueued} | Falhas: {failed}")
    if failures:
        print(f"   📋 Falhas persistidas em: {FAILURES_FILE}")


if __name__ == "__main__":
    asyncio.run(main())
