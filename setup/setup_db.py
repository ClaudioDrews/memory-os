#!/usr/bin/env python3
"""Memory OS — Database Setup Script

Creates the SQLite databases and schemas required by the Icarus plugin.
Run this once during initial installation, or after deleting the databases.

Usage:
    python setup_db.py                          # uses default paths
    python setup_db.py --state-db /custom/path/state.db
    python setup_db.py --memory-db /custom/path/memory_store.db
    python setup_db.py --dry-run                # print SQL without executing

Environment variables:
    STATE_DB_PATH      — path to state.db (default: ~/.hermes/state.db)
    MEMORY_STORE_PATH  — path to memory_store.db (default: ~/.hermes/memory_store.db)
"""

import argparse
import os
import sqlite3
import sys
from pathlib import Path


# ── Default paths (profile-aware) ─────────────────────────────────────────
# Honor HERMES_HOME when set (Hermes sets it to <root>/profiles/<name> for a
# non-default profile) so setup_db creates per-profile databases.
DEFAULT_HERMES_HOME = Path(os.environ.get("HERMES_HOME", "").strip() or (Path.home() / ".hermes"))
DEFAULT_STATE_DB = DEFAULT_HERMES_HOME / "state.db"
DEFAULT_MEMORY_DB = DEFAULT_HERMES_HOME / "memory_store.db"


# ── Schema: state.db ───────────────────────────────────────────────────────

STATE_SCHEMA = """
-- Sessions table — one row per Hermes session
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    user_id TEXT,
    model TEXT,
    model_config TEXT,
    system_prompt TEXT,
    parent_session_id TEXT,
    started_at REAL NOT NULL,
    ended_at REAL,
    end_reason TEXT,
    message_count INTEGER DEFAULT 0,
    tool_call_count INTEGER DEFAULT 0,
    input_tokens INTEGER DEFAULT 0,
    output_tokens INTEGER DEFAULT 0,
    cache_read_tokens INTEGER DEFAULT 0,
    cache_write_tokens INTEGER DEFAULT 0,
    reasoning_tokens INTEGER DEFAULT 0,
    billing_provider TEXT,
    billing_base_url TEXT,
    billing_mode TEXT,
    estimated_cost_usd REAL,
    actual_cost_usd REAL,
    cost_status TEXT,
    cost_source TEXT,
    pricing_version TEXT,
    title TEXT,
    api_call_count INTEGER DEFAULT 0,
    handoff_state TEXT,
    handoff_platform TEXT,
    handoff_error TEXT,
    cwd TEXT,
    rewind_count INTEGER NOT NULL DEFAULT 0,
    archived INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (parent_session_id) REFERENCES sessions(id)
);

-- Messages table — one row per user/assistant/tool message
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions(id),
    role TEXT NOT NULL,
    content TEXT,
    tool_call_id TEXT,
    tool_calls TEXT,
    tool_name TEXT,
    timestamp REAL NOT NULL,
    token_count INTEGER,
    finish_reason TEXT,
    reasoning TEXT,
    reasoning_content TEXT,
    reasoning_details TEXT,
    codex_reasoning_items TEXT,
    codex_message_items TEXT,
    platform_message_id TEXT,
    observed INTEGER DEFAULT 0,
    active INTEGER NOT NULL DEFAULT 1
);

-- FTS5 full-text search over message content (used by session_search)
CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
    content,
    content=messages,
    content_rowid=id
);

-- Trigram FTS5 index for substring/phrase matching
CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts_trigram USING fts5(
    content,
    tokenize='trigram',
    content=messages,
    content_rowid=id
);

-- Lineage tracking — generation provenance for context enhancement
CREATE TABLE IF NOT EXISTS lineage (
    lineage_id TEXT PRIMARY KEY,
    session_id TEXT,
    query TEXT,
    retrieved_chunk_ids TEXT,
    generation_model TEXT,
    generation_context_hash TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Reflection budget — rate-limiting for micro-reflections
CREATE TABLE IF NOT EXISTS reflection_budget (
    hour_window TEXT PRIMARY KEY,
    count INTEGER DEFAULT 0,
    tokens_used INTEGER DEFAULT 0
);

-- Compression locks — concurrency control for session compression
CREATE TABLE IF NOT EXISTS compression_locks (
    session_id TEXT PRIMARY KEY,
    holder TEXT NOT NULL,
    acquired_at REAL NOT NULL,
    expires_at REAL NOT NULL
);

-- Schema version tracking
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER NOT NULL
);

-- State metadata key-value store
CREATE TABLE IF NOT EXISTS state_meta (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""

# ── Schema: memory_store.db ────────────────────────────────────────────────

MEMORY_SCHEMA = """
-- Entities — named entities extracted from facts
CREATE TABLE IF NOT EXISTS entities (
    entity_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL,
    entity_type TEXT DEFAULT 'unknown',
    aliases     TEXT DEFAULT '',
    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Facts — durable structured memory with trust scoring
CREATE TABLE IF NOT EXISTS facts (
    fact_id         INTEGER PRIMARY KEY AUTOINCREMENT,
    content         TEXT NOT NULL UNIQUE,
    category        TEXT DEFAULT 'general',
    tags            TEXT DEFAULT '',
    trust_score     REAL DEFAULT 0.5,
    retrieval_count INTEGER DEFAULT 0,
    helpful_count   INTEGER DEFAULT 0,
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    hrr_vector      BLOB
);

-- Fact-entity junction (many-to-many)
CREATE TABLE IF NOT EXISTS fact_entities (
    fact_id   INTEGER REFERENCES facts(fact_id),
    entity_id INTEGER REFERENCES entities(entity_id),
    PRIMARY KEY (fact_id, entity_id)
);

-- FTS5 full-text search over facts
CREATE VIRTUAL TABLE IF NOT EXISTS facts_fts
    USING fts5(content, tags, content=facts, content_rowid=fact_id);

-- Memory banks — algebraically composable fact groups (HRR vectors)
CREATE TABLE IF NOT EXISTS memory_banks (
    bank_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    bank_name  TEXT NOT NULL UNIQUE,
    vector     BLOB NOT NULL,
    dim        INTEGER NOT NULL,
    fact_count INTEGER DEFAULT 0,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""


def create_database(db_path: Path, schema: str, dry_run: bool = False) -> bool:
    """Create database and execute schema. Returns True on success."""
    print(f"\n📘 Database: {db_path}")

    if dry_run:
        print("   [DRY-RUN] Would execute schema:")
        for line in schema.strip().split("\n"):
            if line.strip():
                print(f"   {line.strip()[:100]}")
        return True

    db_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        conn = sqlite3.connect(str(db_path))
        conn.executescript(schema)
        conn.commit()
        conn.close()
        print(f"   ✅ Created ({db_path.stat().st_size:,} bytes)")

        # Verify tables
        conn = sqlite3.connect(str(db_path))
        tables = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
        conn.close()
        print(f"   📋 Tables: {', '.join(t[0] for t in tables)}")
        return True

    except sqlite3.Error as e:
        print(f"   ❌ Error: {e}", file=sys.stderr)
        return False


def insert_schema_version(db_path: Path, version: int = 1) -> None:
    """Record the schema version if the table exists and is empty."""
    try:
        conn = sqlite3.connect(str(db_path))
        existing = conn.execute(
            "SELECT COUNT(*) FROM schema_version"
        ).fetchone()[0]
        if existing == 0:
            conn.execute(
                "INSERT INTO schema_version (version) VALUES (?)", (version,)
            )
            conn.commit()
        conn.close()
    except sqlite3.Error:
        pass  # table might not exist (not state.db)


def main():
    parser = argparse.ArgumentParser(
        description="Memory OS — Database Setup"
    )
    parser.add_argument(
        "--state-db",
        default=os.environ.get(
            "STATE_DB_PATH", str(DEFAULT_STATE_DB)
        ),
        help=f"Path to state.db (default: {DEFAULT_STATE_DB})",
    )
    parser.add_argument(
        "--memory-db",
        default=os.environ.get(
            "MEMORY_STORE_PATH", str(DEFAULT_MEMORY_DB)
        ),
        help=f"Path to memory_store.db (default: {DEFAULT_MEMORY_DB})",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print SQL without executing",
    )
    args = parser.parse_args()

    state_path = Path(args.state_db)
    memory_path = Path(args.memory_db)

    print("🧠 Memory OS — Database Setup")
    print(f"   Hermes home: {DEFAULT_HERMES_HOME}")
    print(f"   State DB:    {state_path}")
    print(f"   Memory DB:   {memory_path}")

    if args.dry_run:
        print("\n⚠️  DRY-RUN mode — no changes will be made.\n")

    # ── Create state.db ──
    ok_state = create_database(state_path, STATE_SCHEMA, args.dry_run)

    if ok_state and not args.dry_run:
        insert_schema_version(state_path, version=1)

    # ── Create memory_store.db ──
    ok_memory = create_database(memory_path, MEMORY_SCHEMA, args.dry_run)

    # ── Summary ──
    print()
    if args.dry_run:
        print("🏁 Dry-run complete. Run without --dry-run to apply.")
    elif ok_state and ok_memory:
        print("✅ Both databases created successfully.")
        print()
        print("   Next steps:")
        print(f"   1. Ensure Qdrant is running: docker compose up -d qdrant")
        print(f"   2. Create the Qdrant collection:")
        print(f"      python scripts/bulk_wiki_ingest.py   (creates collection automatically)")
        print(f"      OR manually:")
        print(f"      curl -X PUT http://localhost:6333/collections/knowledge_base \\")
        print(f"           -H 'Content-Type: application/json' \\")
        print(f"           -d '{{\"vectors\":{{\"dense\":{{\"size\":4096,\"distance\":\"Cosine\"}}}},\"sparse_vectors\":{{\"sparse\":{{}}}}}}'")
        sys.exit(0)
    else:
        print("❌ Some databases failed to create. Check errors above.")
        sys.exit(1)


if __name__ == "__main__":
    main()
