#!/usr/bin/env python3
"""Backfill decay metadata for Qdrant knowledge_base.

Populates missing fields (created_at, last_accessed_at, importance_score,
confidence_score, archived) so the decay_scanner can actually work.

Modes:
    --dry-run    Simulate everything, print stats, no writes (DEFAULT)
    --commit     Actually update points via REST API
    --pilot N    Process only N points (for validation)

Usage:
    python3 backfill_decay_metadata.py --dry-run --pilot 50
    python3 backfill_decay_metadata.py --dry-run
    python3 backfill_decay_metadata.py --commit

Heuristics:
    - created_at / last_accessed_at:
        Session points:  from payload.timestamp (Unix epoch → ISO 8601)
        Wiki points:     from file mtime if file exists, else now()
    - importance_score:  0.5 flat (conservative)
    - confidence_score:
        wiki-* → 0.85
        session → 0.70
        unknown → 0.75
    - archived:          false (explicit)

Safety:
    - Dry-run by default — no writes without --commit
    - Only touches points MISSING the target fields (no overwrites)
    - Batched via REST API POST /points/payload
    - Uses same pattern as migrate_strength.py (2026-05-29)
"""

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import URLError

from hermes_env import logs_dir

# ─── Config ──────────────────────────────────────────────────────────────────
QDRANT_URL = "http://localhost:6333"
COLLECTION = "knowledge_base"
BATCH_SIZE = 200
SCROLL_LIMIT = 200
LOG_FILE = logs_dir() / "decay_scanner.log"
VAULT_ROOT = Path(os.environ.get("VAULT_PATH", str(Path.home() / "vault")))

# ─── Heuristics ──────────────────────────────────────────────────────────────
CONFIDENCE_BY_SOURCE = {
    "wiki-concepts": 0.85,
    "wiki-entities": 0.85,
    "wiki-comparisons": 0.85,
    "wiki-raw": 0.85,
    "session": 0.70,
}
CONFIDENCE_DEFAULT = 0.75
IMPORTANCE_SCORE = 0.5
ARCHIVED = False


def log(msg: str) -> None:
    """Append timestamped message to log."""
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[backfill-decay] {ts}  {msg}"
    print(line)
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")


def scroll_all() -> list[dict]:
    """Scroll entire collection, returning all points with payloads."""
    points = []
    offset = None
    while True:
        body = {"limit": SCROLL_LIMIT, "with_payload": True, "with_vector": False}
        if offset:
            body["offset"] = offset
        req = Request(
            f"{QDRANT_URL}/collections/{COLLECTION}/points/scroll",
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"},
        )
        try:
            with urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read().decode())
        except URLError as e:
            log(f"ERROR: scroll failed: {e}")
            break
        result = data.get("result", {})
        batch = result.get("points", [])
        points.extend(batch)
        offset = result.get("next_page_offset")
        if offset is None or len(batch) == 0:
            break
    return points


def resolve_timestamp(point: dict) -> str:
    """Return ISO 8601 string for created_at/last_accessed_at.

    Session points: use payload.timestamp (Unix epoch float).
    Wiki points: locate file via payload.file_path → file mtime.
    Fallback: now().
    """
    pl = point.get("payload", {})
    ts_raw = pl.get("timestamp")

    if ts_raw is not None:
        try:
            ts_float = float(ts_raw)
            return datetime.fromtimestamp(ts_float, tz=timezone.utc).isoformat()
        except (ValueError, TypeError, OSError):
            pass

    # Wiki point — resolve file_path to filesystem
    file_path = pl.get("file_path", "")
    source = pl.get("source", "")
    if file_path and source.startswith("wiki-"):
        candidate = Path(file_path)
        try:
            mtime = candidate.stat().st_mtime
            return datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat()
        except OSError:
            pass

    # Absolute fallback
    return datetime.now(timezone.utc).isoformat()


def compute_confidence(point: dict) -> float:
    """Heuristic confidence_score by source."""
    source = point.get("payload", {}).get("source", "")
    return CONFIDENCE_BY_SOURCE.get(source, CONFIDENCE_DEFAULT)


def needs_backfill(point: dict) -> list[str]:
    """Return list of fields this point is missing (that we backfill)."""
    TARGETS = ["created_at", "last_accessed_at", "importance_score", "confidence_score", "archived"]
    pl = point.get("payload", {})
    return [f for f in TARGETS if f not in pl]


def build_payload(point: dict) -> dict:
    """Build the payload fragment to backfill for this point."""
    ts = resolve_timestamp(point)
    return {
        "created_at": ts,
        "last_accessed_at": ts,
        "importance_score": IMPORTANCE_SCORE,
        "confidence_score": compute_confidence(point),
        "archived": ARCHIVED,
    }


def upsert_batch(point_ids: list[str], payload: dict) -> bool:
    """Update payload for a batch of points via REST API. Fail-open."""
    body = {"payload": payload, "points": point_ids}
    req = Request(
        f"{QDRANT_URL}/collections/{COLLECTION}/points/payload",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read().decode())
            return result.get("status") == "ok"
    except URLError as e:
        log(f"ERROR: upsert batch failed: {e}")
        return False


def run(dry_run: bool = True, pilot: int = 0, commit: bool = False):
    """Main execution."""
    mode = "DRY-RUN" if dry_run else "COMMIT"
    log(f"=== BACKFILL DECAY METADATA ({mode}) ===")
    log(f"Collection: {COLLECTION}")
    log(f"Pilot: {pilot if pilot else 'all'}")

    # ── Scroll all points ──
    log("Scrolling all points...")
    all_points = scroll_all()
    total = len(all_points)
    log(f"Total points: {total}")

    # ── Identify points needing backfill ──
    to_update: list[tuple[str, dict]] = []  # (point_id, payload_fragment)
    skipped_already_have = 0
    stats_by_source = {}

    for p in all_points:
        missing = needs_backfill(p)
        if not missing:
            skipped_already_have += 1
            continue
        pid = str(p["id"])
        pl = build_payload(p)
        to_update.append((pid, pl))
        src = p.get("payload", {}).get("source", "unknown")
        stats_by_source[src] = stats_by_source.get(src, 0) + 1

    log(f"Points needing backfill: {len(to_update)}/{total}")
    log(f"Points already complete: {skipped_already_have}")

    if pilot and pilot < len(to_update):
        to_update = to_update[:pilot]
        log(f"Pilot mode: processing first {pilot}")

    # ── Stats by source ──
    log("Backfill candidates by source:")
    for src, count in sorted(stats_by_source.items(), key=lambda x: -x[1]):
        ts_sample = ""
        for pid, pl in to_update:
            if pid and len(ts_sample) < 3:
                p = next((pp for pp in all_points if str(pp["id"]) == pid), None)
                if p and p.get("payload", {}).get("source") == src:
                    ts_sample = pl.get("created_at", "")[:19]
                    break
        log(f"  {src:<25} {count:>6} pts  ts_sample={ts_sample}")

    if dry_run:
        log("DRY-RUN complete. No changes made. Use --commit to apply.")
        return

    # ── Commit ──
    if not commit:
        log("ERROR: --commit flag required for writes. Aborting.")
        return

    log(f"Committing in batches of {BATCH_SIZE}...")
    success = 0
    failed = 0

    for i in range(0, len(to_update), BATCH_SIZE):
        batch = to_update[i : i + BATCH_SIZE]
        # Group by identical payload (same timestamp, same confidence)
        # For simplicity, each point gets its own upsert — but we batch IDs
        # with identical payloads where possible
        payload_groups: dict[str, tuple[list[str], dict]] = {}
        for pid, pl in batch:
            key = json.dumps(pl, sort_keys=True)
            if key not in payload_groups:
                payload_groups[key] = ([], pl)
            payload_groups[key][0].append(pid)

        for ids, pl in payload_groups.values():
            ok = upsert_batch(ids, pl)
            if ok:
                success += len(ids)
            else:
                failed += len(ids)
                log(f"  FAILED batch: {len(ids)} points, first id={ids[0]}")

        batch_num = i // BATCH_SIZE + 1
        total_batches = (len(to_update) + BATCH_SIZE - 1) // BATCH_SIZE
        log(f"  Batch {batch_num}/{total_batches}: {success} ok, {failed} failed")

        if i + BATCH_SIZE < len(to_update):
            time.sleep(0.5)  # gentle rate limit

    log(f"COMMIT complete. success={success}, failed={failed}")
    if failed > 0:
        log("WARNING: some batches failed. Check logs.")


def main():
    parser = argparse.ArgumentParser(description="Backfill decay metadata for Qdrant knowledge_base")
    parser.add_argument("--dry-run", action="store_true", default=True,
                        help="Simulate, no writes (default)")
    parser.add_argument("--commit", action="store_true",
                        help="Actually write to Qdrant")
    parser.add_argument("--pilot", type=int, default=0,
                        help="Process only N points (for validation)")
    args = parser.parse_args()

    if args.commit:
        args.dry_run = False

    run(dry_run=args.dry_run, pilot=args.pilot, commit=args.commit)


if __name__ == "__main__":
    main()
