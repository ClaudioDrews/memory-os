#!/usr/bin/env python3
"""
DLQ Manager — Lê, classifica, reporta e marca falhas do wiki ingest.

Uso:
  python3 dlq_manager.py --report        # reporta falhas não reportadas
  python3 dlq_manager.py --status        # status resumido da DLQ
  python3 dlq_manager.py --json            # saída JSON completa
"""

import os
import sys
import json
import time
from datetime import datetime, timedelta
from typing import List, Dict, Optional
from dataclasses import dataclass, asdict, field
from collections import Counter

# ─── Config (profile-aware) ────────────────────────────────────────────────
# Hermes sets HERMES_HOME to <root>/profiles/<name> when a non-default profile
# is active; hermes_env resolves all Hermes-owned paths from it.
from hermes_env import hermes_home

_HH = hermes_home()
DLQ_PATH = _HH / "wiki_ingest_failures.json"
REPORT_LOG = _HH / "cron" / "output" / "dlq_reports.jsonl"
REPORT_DIR = _HH / "cron" / "output" / "quality_report"
MAX_REPORT_HISTORY = 100  # entradas no JSONL

# ─── Data Model ─────────────────────────────────────────────────────────────

@dataclass
class DLQEntry:
    file: str
    error: str
    timestamp: str
    failure_class: str = "unknown"
    reported: bool = False
    retry_count: int = 0
    last_retry: Optional[str] = None
    error_hash: str = ""  # hash do erro para deduplicação

# ─── File I/O ─────────────────────────────────────────────────────────────

def load_dlq() -> List[DLQEntry]:
    if not os.path.exists(DLQ_PATH):
        return []
    try:
        with open(DLQ_PATH, "r") as f:
            data = json.load(f)
        if isinstance(data, list):
            return [DLQEntry(**item) for item in data]
        elif isinstance(data, dict) and "failures" in data:
            return [DLQEntry(**item) for item in data["failures"]]
        return []
    except Exception as e:
        print(f"[DLQ-ERROR] Falha ao carregar: {e}", file=sys.stderr)
        return []

def save_dlq(entries: List[DLQEntry]):
    tmp = str(DLQ_PATH) + ".tmp"
    with open(tmp, "w") as f:
        json.dump([asdict(e) for e in entries], f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, DLQ_PATH)

# ─── Classification ─────────────────────────────────────────────────────────

def classify_error(error_msg: str) -> str:
    e = error_msg.lower()
    transient = ["timeout", "connection", "temporarily", "rate limit", "503", "502", "504",
                 "too many requests", "unavailable", "cannot", "refused", "reset"]
    permanent = ["404", "not found", "invalid format", "parse error", "file not found",
                 "deleted", "permission denied", "encode", "utf-8", "json", "schema"]
    for p in transient:
        if p in e:
            return "transient"
    for p in permanent:
        if p in e:
            return "permanent"
    return "unknown"

def compute_error_hash(file: str, error: str) -> str:
    """Gera um hash simples para deduplicação de erros similares."""
    import hashlib
    return hashlib.md5(f"{file}:{error[:80]}".encode()).hexdigest()[:8]

# ─── Retry ──────────────────────────────────────────────────────────────────

def retry_transient(entries: List[DLQEntry]) -> int:
    """Re-enable transient failures for retry. Returns count of re-enabled entries."""
    count = 0
    for e in entries:
        if e.failure_class == "transient" and e.reported:
            e.reported = False
            e.retry_count += 1
            e.last_retry = datetime.now().isoformat()
            count += 1
    return count

# ─── Reporting ─────────────────────────────────────────────────────────────

def build_report(entries: List[DLQEntry]) -> Dict:
    unreported = [e for e in entries if not e.reported]
    total = len(entries)
    
    if not unreported:
        return {"status": "ok", "unreported_count": 0, "total": total, "report": ""}
    
    # Classifica
    for e in unreported:
        if e.failure_class == "unknown":
            e.failure_class = classify_error(e.error)
    
    by_class = Counter(e.failure_class for e in unreported)
    by_error_short = Counter(str(e.error)[:70] for e in unreported)
    by_file = Counter(os.path.basename(e.file) for e in unreported)
    
    lines = [
        f"🚨 [DLQ-ALERT] {len(unreported)} nova(s) falha(s) no ingest",
        f"   Total acumulado na DLQ: {total}",
        "",
        "Por classe:",
    ]
    emoji = {"transient": "⏳", "permanent": "💀", "unknown": "❓"}
    for cls, count in by_class.most_common():
        lines.append(f"  {emoji.get(cls, '❓')} {cls}: {count}")
    
    lines.append("")
    lines.append("Top erros:")
    for err, count in by_error_short.most_common(5):
        lines.append(f"  • ({count}x) {err}")
    
    lines.append("")
    lines.append("Arquivos:")
    for fname, count in by_file.most_common(10):
        lines.append(f"  • {fname} ({count}x)")
    
    report_text = "\n".join(lines)
    
    return {
        "status": "alert",
        "unreported_count": len(unreported),
        "total": total,
        "by_class": dict(by_class),
        "top_errors": dict(by_error_short.most_common(5)),
        "report": report_text,
    }

def save_report(report: Dict):
    os.makedirs(REPORT_DIR, exist_ok=True)
    timestamp = datetime.now().isoformat()
    
    # JSONL — append then truncate to MAX_REPORT_HISTORY
    with open(REPORT_LOG, "a") as f:
        f.write(json.dumps({"timestamp": timestamp, **report}, ensure_ascii=False) + "\n")
        f.flush()
        os.fsync(f.fileno())
    
    # Rotate: keep only the last MAX_REPORT_HISTORY entries
    try:
        with open(REPORT_LOG, "r") as f:
            lines = f.readlines()
        if len(lines) > MAX_REPORT_HISTORY:
            with open(REPORT_LOG, "w") as f:
                f.writelines(lines[-MAX_REPORT_HISTORY:])
                f.flush()
                os.fsync(f.fileno())
    except OSError:
        pass  # best-effort rotation

def mark_reported(entries: List[DLQEntry]):
    for e in entries:
        e.reported = True

def get_status_summary(entries: List[DLQEntry]) -> Dict:
    total = len(entries)
    unreported = len([e for e in entries if not e.reported])
    by_class = Counter(e.failure_class for e in entries)
    # Parse timestamps with fallback for malformed values
    now = datetime.now()
    recent = []
    for e in entries:
        try:
            ts = datetime.fromisoformat(e.timestamp.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            continue  # skip entries with unparseable timestamps
        if now - ts < timedelta(hours=24):
            recent.append(e)
    
    return {
        "total": total,
        "unreported": unreported,
        "by_class": dict(by_class),
        "last_24h": len(recent),
        "oldest": entries[0].timestamp if entries else None,
    }

# ─── Main ───────────────────────────────────────────────────────────────────

def main():
    import argparse
    p = argparse.ArgumentParser(description="DLQ Manager — Auto-report de falhas")
    p.add_argument("--report", action="store_true", help="Gerar relatório das não-reportadas")
    p.add_argument("--status", action="store_true", help="Status resumido")
    p.add_argument("--json", action="store_true", help="Saída JSON")
    p.add_argument("--silent-if-ok", action="store_true", help="Silencioso se DLQ ok")
    p.add_argument("--retry", action="store_true", help="Re-enable transient failures for retry")
    args = p.parse_args()
    
    entries = load_dlq()
    
    if args.retry:
        # Classify first so transient detection works
        for e in entries:
            if e.failure_class == "unknown":
                e.failure_class = classify_error(e.error)
        count = retry_transient(entries)
        save_dlq(entries)
        print(f"✅ {count} falha(s) transiente(s) re-habilitadas para retry")
        return
    
    if args.status:
        summary = get_status_summary(entries)
        if args.json:
            print(json.dumps(summary, indent=2, ensure_ascii=False))
        else:
            print(f"DLQ status: {summary['total']} total, {summary['unreported']} não-reportadas")
            for cls, count in summary.get("by_class", {}).items():
                print(f"  {cls}: {count}")
        return
    
    report = build_report(entries)
    
    if report["status"] == "ok":
        msg = "[DLQ-OK] Nenhuma falha nova desde último check."
        if not args.silent_if_ok:
            print(msg)
        if args.json:
            print(json.dumps(report, indent=2, ensure_ascii=False))
        return
    
    # Tem novas falhas
    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
    else:
        print(report["report"])
    
    # Salva e marca como reportadas
    save_report(report)
    mark_reported(entries)
    save_dlq(entries)
    
    # Exit code 1 para cron trigger
    sys.exit(1)

if __name__ == "__main__":
    main()
