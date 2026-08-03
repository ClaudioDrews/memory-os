#!/usr/bin/env python3
"""
reflection_trigger.py
Verifica se o worker ARQ está ocioso (sem jobs pendentes/em execução)
e dispara micro_reflection via enqueue ARQ. Roda via cron a cada 5 minutos.

Regras:
- Só dispara se não há jobs pendentes nem em execução (idle)
- Respeita budget max_per_hour (lê do env ou assume 5)
- Enfileira job ARQ "process_micro_reflection" (função registrada no worker)
- Fail-open: se Redis/ARQ indisponível, sai silenciosamente
- Nunca bloqueia o path crítico de query/ingestão

Uso (cron):
  */5 * * * * /home/calli/ai-stack/cognitive-agent/venv/bin/python /home/calli/ai-stack/scripts/reflection_trigger.py >> /home/calli/.hermes/logs/reflection_trigger.cron.log 2>&1
"""

import os
import sys
import json
import asyncio
import argparse
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from arq import create_pool
from arq.connections import RedisSettings
import redis.asyncio as aioredis

from hermes_env import hermes_home, reflection_log

# ─── Config ────────────────────────────────────────────────────────────────
ENV_PATH = Path.home() / "ai-stack" / "cognitive-agent" / ".env"
if ENV_PATH.exists():
    load_dotenv(ENV_PATH)

REDIS_HOST = os.environ.get("REDIS_HOST", "127.0.0.1")
REDIS_PORT = int(os.environ.get("REDIS_PORT", "6379"))
REDIS_PASSWORD = os.environ.get("REDIS_PASSWORD", "")
MAX_REFLECTIONS_PER_HOUR = int(os.environ.get("MICRO_REFLECTION_MAX_PER_HOUR", "5"))

redis_settings = RedisSettings(
    host=REDIS_HOST,
    port=REDIS_PORT,
    password=REDIS_PASSWORD or None,
)

LOG_FILE = reflection_log()


def log_message(msg: str):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass
    print(line)


async def is_idle() -> bool:
    """Verifica se não há jobs pendentes nem em execução no ARQ."""
    try:
        r = aioredis.Redis(
            host=REDIS_HOST, port=REDIS_PORT,
            password=REDIS_PASSWORD or None,
            decode_responses=True,
        )

        # ARQ armazena jobs em filas tipo 'arq:queue:default'
        queue_names = ["arq:queue:default"]
        qr_prefix = os.environ.get("ARQ_QUEUE_PREFIX", "arq:queue:")
        if qr_prefix:
            try:
                found = await r.keys(f"{qr_prefix}*")
                queue_names = list(found) if found else queue_names
            except Exception:
                pass

        total_pending = 0
        for qn in queue_names:
            try:
                total_pending += await r.llen(qn)
            except Exception:
                pass

        # Jobs em execução: ARQ usa sets tipo 'arq:in-progress:...'
        in_progress_keys = await r.keys("arq:in-progress:*")
        total_in_progress = 0
        for key in in_progress_keys:
            try:
                total_in_progress += await r.scard(key)
            except Exception:
                pass

        await r.aclose()
        return (total_pending + total_in_progress) == 0
    except Exception as e:
        log_message(f"Erro ao verificar idle: {e}")
        return False  # fail-safe: se não conseguir verificar, não dispara


async def check_budget() -> tuple[bool, int, int]:
    """Retorna (permitido, used, max) baseado no contador da hora no SQLite."""
    try:
        import sqlite3
        db_path = hermes_home() / "state.db"
        hour_window = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H")
        conn = sqlite3.connect(str(db_path))
        c = conn.cursor()
        c.execute("SELECT count FROM reflection_budget WHERE hour_window = ?", (hour_window,))
        row = c.fetchone()
        used = row[0] if row else 0
        conn.close()
        return (used < MAX_REFLECTIONS_PER_HOUR, used, MAX_REFLECTIONS_PER_HOUR)
    except Exception as e:
        log_message(f"Erro ao verificar budget: {e}")
        return (True, 0, MAX_REFLECTIONS_PER_HOUR)  # fail-open


def increment_budget():
    """Incrementa o contador de reflections no SQLite."""
    try:
        import sqlite3
        db_path = hermes_home() / "state.db"
        hour_window = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H")
        conn = sqlite3.connect(str(db_path))
        c = conn.cursor()
        c.execute("""
            INSERT INTO reflection_budget (hour_window, count, tokens_used)
            VALUES (?, 1, 0)
            ON CONFLICT(hour_window)
            DO UPDATE SET count = count + 1
        """, (hour_window,))
        conn.commit()
        conn.close()
    except Exception as e:
        log_message(f"Erro ao incrementar budget: {e}")


async def trigger_micro_reflection(dry_run: bool = False) -> dict:
    """Pipeline: idle check → budget check → enqueue ARQ → increment budget."""

    # 1. Idle check
    idle = await is_idle()
    if not idle:
        return {"status": "busy", "triggered": False}

    # 2. Budget check
    budget_ok, used, max_ref = await check_budget()
    if not budget_ok:
        return {
            "status": "budget_exceeded",
            "used": used,
            "max": max_ref,
            "triggered": False,
        }

    # 3. Enqueue
    if dry_run:
        return {
            "status": "would_trigger",
            "triggered": False,
            "used": used,
            "max": max_ref,
        }

    try:
        pool = await create_pool(redis_settings)
        job = await pool.enqueue_job("process_micro_reflection")
        await pool.aclose()

        # 4. Increment budget
        increment_budget()

        return {
            "status": "triggered",
            "triggered": True,
            "job_id": str(job.job_id) if job else None,
            "used": used + 1,
            "max": max_ref,
        }
    except Exception as e:
        log_message(f"Erro ao enfileirar micro_reflection: {e}")
        return {"status": "error", "error": str(e), "triggered": False}


async def main():
    parser = argparse.ArgumentParser(description="Reflection Trigger — idle detection")
    parser.add_argument("--dry-run", action="store_true", help="Simula, não enfileira")
    args = parser.parse_args()

    result = await trigger_micro_reflection(dry_run=args.dry_run)
    log_message(json.dumps(result, ensure_ascii=False))

    if result.get("status") == "error":
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
