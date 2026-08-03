#!/usr/bin/env python3
"""fabric-retrieve.py -- Smart retrieval for fabric entries.

Given a query, returns the top N most relevant entries ranked by:
keyword match, project match, agent match, recency, tier, type match, ref chain.

Usage:
    python3 fabric-retrieve.py "billing issue" --max-results 5 --max-tokens 2000
    python3 fabric-retrieve.py "auth module" --agent icarus --project myapp
"""

import hashlib
import sqlite3

import argparse
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

# Profile-aware paths: resolve through scripts/hermes_env when run standalone,
# else fall back to ~/.hermes for the default profile.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
try:
    from hermes_env import hermes_home, fabric_dir
    _HH = hermes_home
    _FD = fabric_dir
except ImportError:
    _HH = lambda: Path.home() / ".hermes"
    _FD = lambda: Path.home() / "fabric"

FABRIC_DIR = Path(os.environ.get("FABRIC_DIR", str(_FD())))
STATE_DB = Path(os.environ.get("STATE_DB_PATH", str(_HH() / "state.db")))

# ── SQLite fabric index (avoids glob+parse on every retrieval) ──────────

def _ensure_fabric_index(fabric_dir: Path) -> sqlite3.Connection:
    """Return a connection to state.db with a fresh fabric_index table.

    Creates fabric_index + fabric_fts (FTS5) if they don't exist.
    Rebuilds the index when any fabric .md file has an mtime newer than
    the latest stored mtime. Uses a transaction so a partial rebuild
    won't leave the index inconsistent.
    """
    conn = sqlite3.connect(str(STATE_DB))
    conn.row_factory = sqlite3.Row

    conn.executescript("""
        CREATE TABLE IF NOT EXISTS fabric_index (
            entry_id   TEXT PRIMARY KEY,
            agent      TEXT NOT NULL,
            type       TEXT,
            title      TEXT,
            body       TEXT,
            timestamp  TEXT,
            tags       TEXT,
            project    TEXT,
            tier       TEXT,
            status     TEXT,
            assigned_to TEXT,
            refs       TEXT,
            mtime      REAL NOT NULL,
            file_path  TEXT NOT NULL
        );
        CREATE VIRTUAL TABLE IF NOT EXISTS fabric_fts
            USING fts5(title, body, content=fabric_index, content_rowid=rowid);
    """)
    conn.commit()

    # Check freshness
    latest_db_mtime = conn.execute(
        "SELECT COALESCE(MAX(mtime), 0) FROM fabric_index"
    ).fetchone()[0]

    latest_fs_mtime = 0.0
    for d in (fabric_dir, fabric_dir / "cold"):
        try:
            for f in d.glob("*.md"):
                mtime = f.stat().st_mtime
                if mtime > latest_fs_mtime:
                    latest_fs_mtime = mtime
        except OSError:
            pass

    if latest_fs_mtime <= latest_db_mtime:
        return conn  # index is fresh

    # Rebuild in a transaction
    conn.execute("BEGIN")
    conn.execute("DELETE FROM fabric_index")
    conn.execute("DELETE FROM fabric_fts")
    for d in (fabric_dir, fabric_dir / "cold"):
        try:
            for f in d.glob("*.md"):
                e = parse_entry(f)
                if not e:
                    continue
                rel_path = str(f.relative_to(fabric_dir))
                mtime = f.stat().st_mtime
                conn.execute("""
                    INSERT OR REPLACE INTO fabric_index
                    (entry_id, agent, type, title, body, timestamp,
                     tags, project, tier, status, assigned_to, refs,
                     mtime, file_path)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """, (
                    f"{e.get('agent','')}:{e.get('id','')}",
                    e.get("agent", ""),
                    e.get("type", ""),
                    e.get("title", ""),
                    e.get("_body", ""),
                    e.get("timestamp", ""),
                    e.get("tags", ""),
                    e.get("project_id", e.get("project", "")),
                    e.get("tier", ""),
                    e.get("status", ""),
                    e.get("assigned_to", ""),
                    e.get("refs", ""),
                    mtime,
                    rel_path,
                ))
        except OSError:
            pass
    conn.commit()

    return conn


try:
    from .parsing import parse_entry, _strip_generated_obsidian_sections
except ImportError:
    from parsing import parse_entry, _strip_generated_obsidian_sections

STOP_WORDS = {"the", "a", "an", "is", "was", "are", "were", "be", "been", "being",
              "have", "has", "had", "do", "does", "did", "will", "would", "could",
              "should", "may", "might", "shall", "can", "to", "of", "in", "for",
              "on", "with", "at", "by", "from", "as", "into", "through", "during",
              "it", "its", "this", "that", "and", "or", "but", "not", "no", "if",
              "then", "than", "so", "up", "out", "about", "what", "which", "who",
              "how", "when", "where", "why", "i", "me", "my", "we", "our", "you"}

CODE_WORDS = {"function", "bug", "error", "build", "deploy", "test", "code", "fix",
              "commit", "merge", "api", "endpoint", "module", "class", "method",
              "refactor", "debug", "compile", "runtime", "exception", "stack"}

CUSTOMER_WORDS = {"customer", "billing", "support", "ticket", "refund", "account",
                  "subscription", "payment", "invoice", "complaint", "resolution",
                  "escalation", "onboarding", "churn", "retention"}

HANDOFF_WORDS = {"handoff", "review", "reviewer", "pickup", "pending", "relay",
                 "assigned", "assignee", "revise", "revision", "feedback"}


def tokenize(text):
    words = set(re.findall(r'[a-z0-9]+', text.lower()))
    return words - STOP_WORDS


def _ngrams(tokens, n):
    if len(tokens) < n:
        return set()
    return {tuple(tokens[i:i + n]) for i in range(len(tokens) - n + 1)}


def age_hours(timestamp_str):
    if not timestamp_str:
        return 9999
    try:
        ts = datetime.fromisoformat(str(timestamp_str).replace("Z", "+00:00"))
        delta = datetime.now(timezone.utc) - ts
        return max(0, delta.total_seconds() / 3600)
    except (ValueError, AttributeError):
        return 9999


def _as_list(value):
    if not value:
        return []
    if isinstance(value, list):
        return value
    return [value]


def score_entry(entry, query_tokens, agent=None, project=None, relevant_refs=None):
    summary = str(entry.get("summary", ""))
    summary_lower = summary.lower()
    body = (entry.get("_body", "") + " " + summary).lower()
    entry_tokens = tokenize(body)
    summary_tokens = tokenize(summary_lower)
    entry_type = entry.get("type", "")
    query_text = " ".join(re.findall(r"[a-z0-9]+", " ".join(sorted(query_tokens))))
    body_text = " ".join(re.findall(r"[a-z0-9]+", body))
    query_seq = re.findall(r"[a-z0-9]+", query_text)
    body_seq = re.findall(r"[a-z0-9]+", body)

    score = 0.0

    # 1. Keyword match (body + summary)
    keyword_hits = len(query_tokens & entry_tokens)
    score += keyword_hits * 5  # keywords are the primary signal

    # 1a. Summary match is higher-signal than body text alone.
    summary_hits = len(query_tokens & summary_tokens)
    score += summary_hits * 3

    # 1b. Exact phrase and n-gram matches beat loose token overlap.
    raw_query = " ".join(re.findall(r"[a-z0-9]+", entry.get("_query", "")))
    if raw_query and raw_query in body_text:
        score += 18
    if query_seq:
        body_bigrams = _ngrams(body_seq, 2)
        query_bigrams = _ngrams(query_seq, 2)
        score += len(query_bigrams & body_bigrams) * 4
        body_trigrams = _ngrams(body_seq, 3)
        query_trigrams = _ngrams(query_seq, 3)
        score += len(query_trigrams & body_trigrams) * 7

    # 1b. Tag match (tags are high-signal metadata)
    entry_tags = _as_list(entry.get("tags", []))
    tag_tokens = set()
    for t in entry_tags:
        tag_tokens.update(re.findall(r'[a-z0-9]+', str(t).lower()))
    tag_hits = len(query_tokens & tag_tokens)
    score += tag_hits * 4

    # 2. Same project (check project_id field first, then fallback to keyword match)
    entry_project = entry.get("project_id", entry.get("project", ""))
    if project:
        project_lower = project.lower()
        if project_lower == str(entry_project).lower():
            score += 10  # exact project_id match
        elif project_lower in body:
            score += 8   # project name in body text
        elif any(project_lower in str(t).lower() for t in entry_tags):
            score += 8   # project name in tags

    # 3. Same agent
    if agent and entry.get("agent") == agent:
        score += 5

    # 4. Recency (secondary signal, should not override keyword match)
    hours = age_hours(entry.get("timestamp"))
    if hours < 1:
        score += 4
    elif hours < 24:
        score += 3
    elif hours < 168:  # 1 week
        score += 2
    elif hours < 720:  # 1 month
        score += 1

    # 5. Tier boost (light touch)
    tier = entry.get("tier", "")
    if tier == "hot":
        score += 2
    elif tier == "warm":
        score += 1

    # 6. Type match
    if query_tokens & CODE_WORDS:
        if entry_type in ("code-session", "review", "decision"):
            score += 5
    if query_tokens & CUSTOMER_WORDS:
        if entry_type in ("resolution", "task", "decision"):
            score += 5
    if query_tokens & HANDOFF_WORDS:
        if entry_type in ("task", "review", "resolution", "code-session"):
            score += 6
        elif entry_type == "session":
            score -= 3

    # Prefer source work artifacts over generic summaries.
    if entry_type in ("task", "review", "resolution", "code-session", "research"):
        score += 2
    elif entry_type == "decision":
        score += 1
    elif entry_type == "session":
        score -= 2

    # Structured workflow fields are high-signal for handoffs and follow-ups.
    if entry.get("status") == "open":
        score += 4
    if entry.get("assigned_to"):
        score += 2

    # Reviews reference the original work's keywords, which inflates their
    # keyword score. When the query isn't asking for reviews/feedback, penalize
    # type=review so the source entry ranks higher.
    REVIEW_QUERY_WORDS = {"review", "reviewed", "feedback", "issue", "fix", "must", "should", "approve", "reject", "lgtm"}
    if entry_type == "review":
        if query_tokens & REVIEW_QUERY_WORDS:
            # query IS about reviews — boost linking fields
            if entry.get("review_of"):
                score += 5
        else:
            # query is about the work itself — reviews are secondary
            score -= 4
    elif entry.get("review_of"):
        score += 3
    if entry.get("revises"):
        score += 4

    # 7. Ref chain
    if relevant_refs:
        entry_refs = _as_list(entry.get("refs", []))
        entry_id = entry.get("id", "")
        entry_agent = entry.get("agent", "")
        entry_cycle = str(entry.get("cycle", ""))
        for ref in relevant_refs:
            if ref in entry_refs:
                score += 3
        # Check if this entry is referenced by a relevant entry
        for ref_str in relevant_refs:
            if ":" in ref_str:
                ref_agent, ref_id = ref_str.split(":", 1)
                if ref_agent == entry_agent and (ref_id == entry_id or ref_id == entry_cycle):
                    score += 3
        linked_refs = [entry.get("review_of"), entry.get("revises")]
        for ref in linked_refs:
            if ref and ref in relevant_refs:
                score += 5

    return score


def deduplicate(entries):
    seen = {}
    result = []
    for e in entries:
        body = e.get("_body", "") or ""
        key = hashlib.sha256(body.encode()).hexdigest()
        existing = seen.get(key)
        if existing:
            # Keep the newer one
            if str(e.get("timestamp", "")) > str(existing.get("timestamp", "")):
                result.remove(existing)
                result.append(e)
                seen[key] = e
        else:
            seen[key] = e
            result.append(e)
    return result


def retrieve(query, max_results=5, max_tokens=2000, agent=None, project=None):
    if not FABRIC_DIR.exists():
        return []

    # Use SQLite index instead of glob+parse
    conn = _ensure_fabric_index(FABRIC_DIR)

    rows = conn.execute("""
        SELECT entry_id, agent, type, title, body, timestamp, tags,
               project, tier, status, assigned_to, refs, mtime, file_path
        FROM fabric_index
        ORDER BY mtime DESC
    """).fetchall()

    if not rows:
        return []

    # Convert rows to entry dicts matching score_entry field names
    entries = []
    for row in rows:
        e = dict(row)
        e["_body"] = e.pop("body", "")
        e["_file"] = e.get("file_path", "")
        eid_parts = (e.get("entry_id") or ":").split(":", 1)
        e["id"] = eid_parts[1] if len(eid_parts) > 1 else ""
        entries.append(e)

    if not entries:
        return []

    query_tokens = tokenize(query)
    query_words = re.findall(r"[a-z0-9]+", query.lower())
    normalized_query = " ".join(query_words)
    for e in entries:
        e["_query"] = normalized_query

    # First pass: score without ref chain
    scored = [(score_entry(e, query_tokens, agent, project), e) for e in entries]

    # Collect refs from top-scoring entries for ref chain boost
    scored.sort(key=lambda x: x[0], reverse=True)
    top_refs = set()
    for score, e in scored[:10]:
        if score > 0:
            refs = e.get("refs", [])
            if isinstance(refs, str):
                refs = [refs]
            top_refs.update(refs)
            eid = e.get("id", "")
            eagent = e.get("agent", "")
            if eid:
                top_refs.add(f"{eagent}:{eid}")

    # Second pass: rescore with ref chain
    if top_refs:
        scored = [(score_entry(e, query_tokens, agent, project, top_refs), e) for e in entries]
        scored.sort(key=lambda x: x[0], reverse=True)

    # Filter zero scores
    scored = [(s, e) for s, e in scored if s > 0]

    # Deduplicate
    deduped_entries = deduplicate([e for _, e in scored])
    # Reattach scores
    score_map = {id(e): s for s, e in scored}
    # Rebuild scored list preserving dedup order
    final = []
    for e in deduped_entries:
        # Find the score for this entry
        for s, orig in scored:
            if orig is e:
                final.append((s, e))
                break
    final.sort(key=lambda x: x[0], reverse=True)

    # Apply max_results
    final = final[:max_results]

    # Apply token budget (always enforced, including first entry)
    budget = max_tokens
    result = []
    for score, e in final:
        content = e.get("_full", "")
        tokens = len(content) // 4
        if tokens > budget:
            continue  # skip oversized entries, try next
        budget -= tokens
        result.append((score, e))

    return result


def format_results(results):
    lines = []
    for score, e in results:
        lines.append(f"# relevance: {score:.0f} | {e.get('agent','')} | {e.get('platform','')} | {e.get('type','')}")
        lines.append(e.get("_full", "").strip())
        lines.append("---")
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="Smart fabric retrieval")
    parser.add_argument("query", help="Search query or current task description")
    parser.add_argument("--max-results", type=int, default=5, help="Maximum entries to return")
    parser.add_argument("--max-tokens", type=int, default=2000, help="Token budget (chars/4)")
    parser.add_argument("--agent", default=None, help="Boost entries from this agent")
    parser.add_argument("--project", default=None, help="Boost entries from this project")
    parser.add_argument("--fabric-dir", default=None, help="Override fabric directory")
    args = parser.parse_args()

    global FABRIC_DIR
    if args.fabric_dir:
        FABRIC_DIR = Path(args.fabric_dir)

    results = retrieve(args.query, args.max_results, args.max_tokens, args.agent, args.project)

    if not results:
        print("no relevant entries found")
        sys.exit(0)

    print(format_results(results))


if __name__ == "__main__":
    main()
