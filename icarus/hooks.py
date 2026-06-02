"""Lifecycle hooks — memory capture, decision detection, creative tracking."""

import json
import logging
import os
import re
import urllib.request
import urllib.error
from datetime import datetime
from pathlib import Path

from . import state

# ── LLM extraction key ──
_OPENROUTER_KEY = (
    os.environ.get("OPENROUTER_FULL_API_KEY", "")
    or os.environ.get("OPENROUTER_DS_API_KEY", "")
    or os.environ.get("OPENROUTER_API_KEY", "")
)
_EXTRACTION_MODEL = os.environ.get("ICARUS_EXTRACTION_MODEL", "deepseek/deepseek-v4-flash")
_EXTRACTION_MAX_TOKENS = int(os.environ.get("ICARUS_EXTRACTION_MAX_TOKENS", "1024"))

logger = logging.getLogger(__name__)

# ── Truncation limits (env-configurable) ──
_RESULT_MAX = int(os.environ.get("ICARUS_RESULT_MAX_CHARS", "500"))
_TASK_MAX = int(os.environ.get("ICARUS_TASK_MAX_CHARS", "300"))

# ── System injection detection ──
_SYSTEM_PREFIXES = (
    "[IMPORTANT:",
    "[SYSTEM:",
    "You are running as a scheduled",
)


def _is_system_injection(text):
    """Return True if text starts with a known orchestrator/system preamble."""
    stripped = text.strip()
    return any(stripped.startswith(p) for p in _SYSTEM_PREFIXES)

# use shared regexes from state for decision/outcome/completion detection
# keep local regexes only for creative tracking (broader set)
_THEME_RE = re.compile(
    r"(?i)\b(decided|resolved|completed|fixed|deployed|shipped|reviewed|approved|rejected|built|created)\b"
)
_EVAL_RE = re.compile(
    r"(?i)\b(worked well|didn't work|failed|succeeded|learned|noticed|realized|discovered|finding|insight|improvement)\b"
)
_QUESTION_RE = re.compile(
    r"(?i)\b(what if|wonder|curious about|want to try|experiment with|explore|investigate|test whether)\b"
)
_STOPWORDS = frozenset(
    "this that with from have been were will about would could should their there "
    "these them then when what which some other more also just like very into only "
    "than over such make made most each does done being".split()
)

# ── Topic overlap tracking ──
_last_query_tokens: set = set()

# ── Per-session injection dedup (reset on session start) ──
_injected_fabric: set = set()
_injected_qdrant: set = set()
_injected_sessions: set = set()


def _tokenize(text):
    words = set(re.findall(r"[a-z0-9]+", text.lower()))
    return words - {"the", "a", "an", "is", "was", "are", "to", "of", "in", "for",
                    "on", "with", "it", "and", "or", "not", "i", "you", "can", "do",
                    "this", "that", "what", "how", "please", "help", "me", "my"}


def _extract_theme(text):
    words = re.findall(r"\b[a-z]{4,}\b", text.lower())
    filtered = [w for w in words[:30] if w not in _STOPWORDS][:3]
    return " ".join(filtered) if filtered else ""


def _sanitize_learning(s: str) -> str:
    """Remove unpaired backticks that would produce orphaned markdown."""
    if s.count('`') % 2 != 0:
        s = s.replace('`', '')
    if s.count('```') % 2 != 0:
        s = s.replace('```', '')
    return s.strip()


def _extract_sentence(text, pattern):
    for s in re.split(r"[.!?\n]+", text):
        s = s.strip()
        if len(s) > 15 and pattern.search(s):
            return s[:120]
    return ""


# ── Hooks ────────────────────────────────────────────────

def on_session_start(session_id="", platform="", **kwargs):
    """Load context: SOUL + pending handoffs + recent entries + creative state."""
    global _last_query_tokens
    _last_query_tokens = set()
    _injected_fabric.clear()
    _injected_qdrant.clear()
    _injected_sessions.clear()
    state.session_id = session_id
    state.exchanges = []
    state._recall_log = []

    creative = state.load_creative()
    creative["cycle"] += 1
    state.save_creative(creative)

    parts = []

    soul = state.load_soul()
    if soul:
        parts.append(soul.strip())

    # pending work (handoff-aware)
    open_tasks, reviews, open_tickets = state.read_pending()
    if open_tasks:
        parts.append(f"[fabric] {len(open_tasks)} item(s) assigned to you:")
        for t in open_tasks[:5]:
            src = t.get("agent", "?")
            entry_id = t.get("id", "?")
            etype = t.get("type", "task")
            parts.append(f"  - {src}: {t.get('summary', '?')} ({etype}, id {entry_id})")
        parts.append("  If reviewing, set review_of. If revising, set revises. Otherwise just complete the work.")

    if reviews:
        parts.append(f"[fabric] {len(reviews)} review(s) of your work:")
        for r in reviews[:5]:
            reviewer = r.get("agent", "?")
            entry_id = r.get("id", "?")
            ref = r.get("review_of", "")
            parts.append(f"  - {reviewer}: {r.get('summary', '?')} (review id {entry_id}, of {ref})")
        parts.append("  When you fix the issues, set revises to your original entry's agent:id.")

    if open_tickets:
        parts.append(f"[fabric] {len(open_tickets)} ticket(s) assigned to you:")
        for t in open_tickets[:5]:
            cid = t.get("customer_id", "?")
            src = t.get("agent", "?")
            entry_id = t.get("id", "?")
            parts.append(f"  - [{cid}] {t.get('summary', '?')} (from {src}, id {entry_id})")
        parts.append("  Carry customer_id forward when you resolve these.")

    # cross-agent feedback (non-pending items)
    if not open_tasks and not reviews:
        feedback = state.read_cross_agent(3)
        if feedback:
            parts.append("[fabric] from other agents:")
            for f in feedback:
                parts.append(f"  {f}")

    # recent entries
    entries = state.read_recent(limit=5)
    if entries:
        parts.append("[fabric] recent activity:")
        for e in entries:
            ts = e["timestamp"][:16] if e["timestamp"] else "?"
            parts.append(f"  [{ts}] {e['agent']}: {e['summary']}")

    # creative state
    if creative["questions"]:
        parts.append(f"[fabric] open questions: {'; '.join(creative['questions'][-3:])}")
    if creative["learnings"]:
        parts.append(f"[fabric] learnings: {'; '.join(creative['learnings'][-3:])}")

    context = "\n".join(parts)
    return {"context": context} if context else None


# ── Qdrant context injection ──────────────────────────────

_SOCIAL_CLOSERS = frozenset({
    "ok", "obrigado", "valeu", "beleza", "blz", "tks", "thanks",
    "👍", "👌", "✅", "feito", "certo", "confirmo", "entendido",
    "certo", "isso", "sim", "não", "claro", "perfeito", "ótimo"
})


def _is_social_close(text):
    """Return True if message is a social closer that shouldn't trigger search."""
    stripped = text.strip().lower()
    if stripped in _SOCIAL_CLOSERS:
        return True
    # Very short ASCII-only without technical markers
    if len(stripped) < 6 and stripped.isascii() and not any(
        c in stripped for c in "://.@#$_?"
    ):
        return True
    return False


def _search_qdrant(query, top_k=2, threshold=0.72):
    """Search Qdrant knowledge_base via context_enhancer pipeline.

    Returns list of result dicts with keys: id, score, title,
    content_preview, source, tags.
    Returns empty list on any failure (fail-open).
    """
    try:
        # context_enhancer looks for OPENROUTER_API_KEY (singular);
        # inject our resolved key into environ for compatibility
        if _OPENROUTER_KEY and not os.environ.get("OPENROUTER_API_KEY"):
            os.environ["OPENROUTER_API_KEY"] = _OPENROUTER_KEY

        from scripts.context_enhancer import (
            embed_query, embed_query_sparse, search_with_fallback
        )
        dense = embed_query(query)
        sparse = embed_query_sparse(query)
        results, _level, _qdrant_ms, _fallback_ms = search_with_fallback(
            dense_vector=dense,
            sparse_vector=sparse,
            query_text=query,
            top_k=top_k,
            score_threshold=threshold,
        )
        return results
    except Exception:
        return []


# ── Session history search (FTS5 over state.db) ──────────────


def _resolve_state_db():
    """Locate the Hermes session DB. Prefer state.HERMES_HOME, fall back to ~/.hermes."""
    import sqlite3  # noqa: F401 (ensure available)
    candidates = []
    home = getattr(state, "HERMES_HOME", None)
    if home:
        candidates.append(Path(home) / "state.db")
    candidates.append(Path.home() / ".hermes" / "state.db")
    for c in candidates:
        if c and c.exists():
            return c
    return None


def _search_sessions(query, current_session_id="", top_k=2):
    """FTS5 search over prior session messages in state.db.

    Returns list of {session_id, title, when, snippet}, excluding the
    current session. Fail-open: returns [] on any error.
    """
    import sqlite3
    db = _resolve_state_db()
    if not db:
        return []

    # Build an FTS5 OR-query from meaningful tokens (avoids AND over-filtering)
    toks = [t for t in _tokenize(query) if len(t) >= 4]
    if not toks:
        return []
    fts_query = " OR ".join(toks[:8])

    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        cur = con.cursor()
        # NOTE: snippet() cannot be combined with GROUP BY in the same SELECT
        # (FTS5 raises "unable to use function snippet in the requested
        # context"). Fetch top-ranked rows and dedup by session in Python.
        rows = cur.execute(
            """
            SELECT m.session_id AS session_id,
                   s.title       AS title,
                   s.started_at  AS started_at,
                   snippet(messages_fts, 0, '', '', '…', 12) AS snip
            FROM messages_fts
            JOIN messages m ON m.id = messages_fts.rowid
            LEFT JOIN sessions s ON s.id = m.session_id
            WHERE messages_fts MATCH ?
              AND m.session_id != ?
              AND m.role IN ('user','assistant')
            ORDER BY rank
            LIMIT 20
            """,
            (fts_query, current_session_id),
        ).fetchall()
        con.close()
    except Exception:
        return []

    out = []
    seen = set()
    for r in rows:
        sid = r["session_id"]
        if sid in seen:
            continue
        seen.add(sid)
        # started_at is a Unix timestamp (float); format to a readable date.
        when = ""
        sa = r["started_at"]
        if sa:
            try:
                when = datetime.fromtimestamp(float(sa)).strftime("%Y-%m-%d %H:%M")
            except (ValueError, TypeError, OSError):
                when = str(sa)[:16]
        out.append({
            "session_id": sid,
            "title": r["title"],
            "when": when,
            "snippet": (r["snip"] or "").replace("\n", " "),
        })
        if len(out) >= top_k:
            break
    return out


# ── fact_store search (FTS5 over memory_store.db) ────────────

def _search_facts(query, top_k=3):
    """FTS5 search over durable facts in memory_store.db.

    Returns list of fact content strings. Fail-open: returns [] on error.
    """
    import sqlite3
    db = Path.home() / ".hermes" / "memory_store.db"
    home = getattr(state, "HERMES_HOME", None)
    if home and (Path(home) / "memory_store.db").exists():
        db = Path(home) / "memory_store.db"
    if not db.exists():
        return []

    toks = [t for t in _tokenize(query) if len(t) >= 4]
    if not toks:
        return []
    fts_query = " OR ".join(toks[:8])

    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        cur = con.cursor()
        rows = cur.execute(
            """
            SELECT f.content AS content, f.trust_score AS trust
            FROM facts_fts
            JOIN facts f ON f.fact_id = facts_fts.rowid
            WHERE facts_fts MATCH ?
            ORDER BY rank
            LIMIT ?
            """,
            (fts_query, top_k),
        ).fetchall()
        con.close()
    except Exception:
        return []

    return [r["content"][:200] for r in rows if r["content"]]


# ── Prompt injection sanitization ────────────────────────────

_INJECTION_PATTERNS = [
    # "ignore all previous/prior instructions/directives"
    (re.compile(r"(?i)\bignore\s+all\s+(previous|prior)\s+(instructions|directives|commands|messages|prompts|context)"),
     "[REDACTED]"),
    # "you are/will now become/act/acting as (a/an) AI/assistant..."
    (re.compile(r"(?i)\byou\s+(are|will\s+now)\s+(now\s+)?(become|act|acting)\s+as\s+(a\s+|an\s+)?(AI\s+assistant|assistant|AI|agent|LLM|chatbot|model|system)"),
     "[REDACTED]"),
    # "new instructions/directives/commands follow/above/below"
    (re.compile(r"(?i)\bnew\s+(instructions|directives|commands)\s+(follow|above|below)"),
     "[REDACTED]"),
    # Template injection: {{...}}, ${...}
    (re.compile(r"\{\{.*?\}\}|\$\{.*?\}"), "[REDACTED]"),
    # Triple-backtick code fences
    (re.compile(r"```"), "[code]"),
    # Markdown/javascript data: URLs in links and images
    (re.compile(r"(?i)(javascript|data)\s*:"), "sanitized:"),
    # XML/HTML injection: <script>, event handlers, iframes
    (re.compile(r"<\s*script[\s>]|on\w+\s*=|<\s*iframe[\s>]"), "[sanitized]"),
    # Known system prefixes
    (re.compile(r"(?i)\[IMPORTANT:.*?\]|\[SYSTEM:.*?\]|\[OVERRIDE:.*?\]"), ""),
    # Control characters (keep newlines and tabs)
    (re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]"), ""),
    # Zero-width and invisible Unicode
    (re.compile(r"[\u200b-\u200f\u2028-\u202f\u2060-\u2064\ufeff]"), ""),
]


def _validate_safe_content(text: str) -> str:
    """Catch unknown attack patterns via heuristic:
    high density of directive/imperative language in a short span.
    Falls back to [SANITIZED] placeholder if heuristic triggers.
    """
    if not text or len(text) < 20:
        return text
    try:
        # Count directive-style phrases per character
        directivess = len(re.findall(
            r"(?i)\b(ignore|forget|disregard|override|replace|pretend|act\s+as|you\s+(are|must|will|shall))\b",
            text
        ))
        if directivess >= 3 and directivess / max(len(text), 1) > 0.02:
            return "[SANITIZED]"
        return text
    except Exception:
        return text


def _sanitize_context_text(text: str, max_len: int = 600) -> str:
    """Sanitize retrieved text before it enters the agent's context.
    Strips known injection patterns, validates safety, truncates.
    Fail-open: returns truncated original on error.
    """
    if not text:
        return ""
    try:
        result = str(text)
        for pattern, replacement in _INJECTION_PATTERNS:
            result = pattern.sub(replacement, result)
        # Safety heuristic catch
        result = _validate_safe_content(result)
        # Normalize excessive whitespace
        result = re.sub(r"\n{4,}", "\n\n\n", result)
        result = re.sub(r" {8,}", " ", result)
        return result.strip()[:max_len]
    except Exception:
        return str(text)[:max_len]


def pre_llm_call(session_id="", user_message="", is_first_turn=False, **kwargs):
    """Inject relevant memories when topic changes (fabric + Qdrant)."""
    global _last_query_tokens
    if not user_message:
        return None

    tokens = _tokenize(user_message)
    if not tokens:
        return None

    # Overlap gate: only suppress on NEAR-LITERAL repetition of the previous
    # turn (>0.85). The old 0.6 gate killed all injection in long single-topic
    # sessions — exactly when accumulated context matters most. We now keep
    # injecting and rely on per-source dedup (_injected_* sets) to avoid
    # repeating identical results turn after turn.
    if _last_query_tokens:
        overlap = len(tokens & _last_query_tokens) / max(len(tokens), 1)
        if overlap > 0.85:
            return None

    _last_query_tokens = tokens

    is_social = _is_social_close(user_message)

    agent = state.AGENT_NAME or "agent"
    results = state.recall(user_message, max_results=5, agent=agent)

    # log fabric recall for telemetry (even if empty)
    if results:
        state.log_recall(user_message, results, source="pre_llm_call")

    # ── Qdrant search (independent of fabric) ──
    # Threshold lowered 0.72 → 0.55: legitimate queries scored 0.57-0.63 and
    # were silently filtered out by the old 0.72 gate.
    qdrant_results = []
    if not is_social:
        qdrant_results = _search_qdrant(user_message, top_k=2, threshold=0.55)

    # ── Session history (FTS5 over state.db) — the layer that holds
    #    "this was already built in a prior session". No automatic injection
    #    existed before; this is new. ──
    session_results = []
    if not is_social:
        session_results = _search_sessions(user_message, session_id, top_k=2)

    # ── fact_store probe (durable user/environment facts) — first turn only,
    #    to avoid per-turn cost. ──
    fact_results = []
    if is_first_turn and not is_social:
        fact_results = _search_facts(user_message, top_k=3)

    # ── Bail if nothing from any source ──
    if not results and not qdrant_results and not session_results and not fact_results:
        return None

    parts = []

    # Fabric context (dedup against previously injected entry ids)
    if results:
        lines = ["[fabric] relevant to your request:"]
        emitted = 0
        for e in results:
            summary = _sanitize_context_text(
                e.get("summary") or e.get("_body", e.get("body", "")), max_len=80
            )
            eid = str(e.get("id", "")) or summary[:60]
            if eid in _injected_fabric:
                continue
            _injected_fabric.add(eid)
            ts = str(e.get("timestamp", ""))[:16] or "?"
            lines.append(f"  [{ts}] {e.get('agent', '?')}: {summary}")
            emitted += 1
        if emitted:
            parts.append("\n".join(lines))

    # Qdrant context (dedup against previously injected point ids)
    if qdrant_results:
        lines = ["[qdrant] knowledge base:"]
        emitted = 0
        for r in qdrant_results:
            rid = str(r.get("id", "")) or str(r.get("content_preview", ""))[:40]
            if rid in _injected_qdrant:
                continue
            _injected_qdrant.add(rid)
            source = r.get("source", "?")
            title = r.get("title", "")
            score = r.get("score", 0)
            label = f"{source}"
            if title:
                label = f"{source}: {title[:60]}"
            content = _sanitize_context_text(r.get("content_preview", ""))
            lines.append(f"  ### {label} (score: {score:.2f})\n  {content}")
            emitted += 1
        if emitted:
            parts.append("\n".join(lines))

    # Session history context (dedup against previously injected session ids)
    if session_results:
        lines = ["[sessions] prior conversations on this topic:"]
        emitted = 0
        for s in session_results:
            sid = s.get("session_id", "")
            if sid in _injected_sessions:
                continue
            _injected_sessions.add(sid)
            title = s.get("title") or "(untitled)"
            snippet = _sanitize_context_text(s.get("snippet", ""), max_len=200)
            when = s.get("when", "")
            lines.append(f"  [{when}] {title}: {snippet}")
            emitted += 1
        if emitted:
            parts.append("\n".join(lines))

    # fact_store context (first turn only)
    if fact_results:
        lines = ["[facts] durable facts about the user/environment:"]
        for f in fact_results:
            lines.append(f"  - {_sanitize_context_text(f, max_len=200)}")
        parts.append("\n".join(lines))

    if not parts:
        return None

    return {"context": "\n\n".join(parts)}


def post_llm_call(session_id="", user_message="", assistant_response="", platform="", **kwargs):
    """Capture high-value decisions + creative tracking."""
    if not assistant_response:
        return

    state.exchanges.append({
        "user": (user_message or "")[:200],
        "assistant": assistant_response[:500],
    })

    agent = state.AGENT_NAME or "agent"
    plat = platform or "cli"

    # capture decisions: requires decision + outcome in response, AND a substantial
    # user request (>50 chars) to ground the claim
    user_text = (user_message or "").strip()
    if (state.DECISION_RE.search(assistant_response)
            and state.OUTCOME_RE.search(assistant_response)
            and len(assistant_response) > 200
            and len(user_text) > 50):
        body = f"Task: {user_text[:_TASK_MAX]}\n\nResult: {assistant_response[:_RESULT_MAX]}"
        summary = assistant_response[:80].replace("\n", " ")
        entry_status = "completed" if state.COMPLETION_RE.search(assistant_response) else ""
        state.write_entry("decision", body, summary,
                         platform=plat, status=entry_status, training_value="high")

    # creative tracking (uses broader _THEME_RE, doesn't write entries)
    creative = state.load_creative()
    changed = False

    if _THEME_RE.search(assistant_response):
        theme = _extract_theme(assistant_response)
        if theme and theme not in creative["themes"]:
            creative["themes"].append(theme)
            creative["themes"] = creative["themes"][-20:]
            changed = True

    if _EVAL_RE.search(assistant_response):
        learning = _extract_sentence(assistant_response, _EVAL_RE)
        if learning:
            learning = _sanitize_learning(learning)
        if learning and learning not in creative["learnings"]:
            creative["learnings"].append(learning)
            creative["learnings"] = creative["learnings"][-15:]
            changed = True

    if _QUESTION_RE.search(assistant_response):
        question = _extract_sentence(assistant_response, _QUESTION_RE)
        if question and question not in creative["questions"]:
            creative["questions"].append(question)
            creative["questions"] = creative["questions"][-15:]
            changed = True

    if changed:
        state.save_creative(creative)


# ── LLM-powered session extraction ────────────────────────

def _parse_json_robust(raw):
    """Extract JSON array/object from LLM output with markdown tolerances.

    Handles: ```json fences, leading text, trailing commas, whitespace.
    Returns parsed value on success, None on failure.
    """
    if not raw or not raw.strip():
        return None

    text = raw.strip()

    # Strip markdown code fences
    for fence in ("```json", "```"):
        if text.startswith(fence):
            text = text[len(fence):].lstrip()
        if text.endswith("```"):
            text = text[:-3].rstrip()

    # Find first JSON structure character
    for start_char in ("[", "{"):
        idx = text.find(start_char)
        if idx != -1:
            text = text[idx:]
            break

    # Attempt parse; progressively strip trailing characters on failure
    attempts = 0
    while attempts < 20:
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            # Strip last char and try again (handles trailing commas, extra })
            if text:
                text = text[:-1]
            attempts += 1
            continue

    return None


def _build_transcript(exchanges):
    """Build a compact transcript from session exchanges for LLM analysis."""
    lines = []
    for i, ex in enumerate(exchanges):
        user = (ex.get("user") or "").strip()
        assistant = (ex.get("assistant") or "").strip()
        if user:
            lines.append(f"[Turn {i+1} — User]\n{user[:500]}")
        if assistant:
            lines.append(f"[Turn {i+1} — Agent]\n{assistant[:800]}")
    return "\n\n".join(lines)


def _llm_extract_entries(transcript):
    """Use LLM to extract significant entries from session transcript.

    Returns list of dicts: {type, summary, content, training_value}
    Returns empty list on failure or if nothing worth preserving.
    """
    if not _OPENROUTER_KEY:
        logger.warning("icarus: no OpenRouter key — skipping LLM extraction")
        return []

    prompt = (
        "You are a session archivist for an AI agent. Analyze this agent session "
        "transcript and extract ONLY significant entries worth preserving in a "
        "cross-agent knowledge base. Skip trivial sessions, greetings, and routine chatter.\n\n"
        "For each significant entry, provide:\n"
        "- type: \"decision\" (technical decision with rationale), "
        "\"resolution\" (bug fix or problem solved), "
        "or \"note\" (discovery or learning)\n"
        "- summary: one line, max 80 chars, in the original language of the session\n"
        "- content: structured markdown with ## Context, ## Action/Decision, and ## Outcome. "
        "Include concrete details: commands, paths, error messages, decisions made.\n"
        "- training_value: \"high\" (outcome verified, artifact produced, decision with evidence), "
        "\"normal\" (useful context or progress), "
        "or \"low\" (marginal, but not zero)\n\n"
        "If the session contains NOTHING worth preserving across sessions, "
        "return an empty array: []\n\n"
        "Return ONLY valid JSON array, no other text:\n"
        '[{"type": "decision", "summary": "...", "content": "...", "training_value": "high"}, ...]'
    )

    payload = json.dumps({
        "model": _EXTRACTION_MODEL,
        "messages": [
            {"role": "system", "content": prompt},
            {"role": "user", "content": transcript[:8000]}
        ],
        "max_tokens": _EXTRACTION_MAX_TOKENS,
        "temperature": 0.2
    }).encode("utf-8")

    try:
        req = urllib.request.Request(
            "https://openrouter.ai/api/v1/chat/completions",
            data=payload,
            headers={
                "Authorization": f"Bearer {_OPENROUTER_KEY}",
                "Content-Type": "application/json",
                "HTTP-Referer": "https://hermes-agent.local",
                "X-Title": "Icarus Session Extraction"
            }
        )
        resp = urllib.request.urlopen(req, timeout=45)
        body = json.loads(resp.read().decode("utf-8"))
        raw = body["choices"][0]["message"]["content"]

        # Parse JSON from response (robust — handles markdown fences, null)
        if raw is None:
            raise ValueError("DeepSeek returned content:null (response_format bug)")
        extracted = _parse_json_robust(raw)
        if isinstance(extracted, dict):
            # Some models return {entries: [...]} — unwrap
            for key in ("entries", "results", "items"):
                if key in extracted and isinstance(extracted[key], list):
                    extracted = extracted[key]
                    break
            else:
                # Single entry wrapped in dict
                if "type" in extracted:
                    extracted = [extracted]
                else:
                    extracted = []

        if not isinstance(extracted, list):
            logger.warning("icarus: LLM extraction returned non-list: %s", type(extracted))
            return []

        # Validate and filter
        valid = []
        allowed_types = {"decision", "resolution", "note"}
        for entry in extracted:
            if not isinstance(entry, dict):
                continue
            etype = entry.get("type", "")
            summary = entry.get("summary", "")
            content = entry.get("content", "")
            if etype not in allowed_types:
                continue
            if len(summary) < 10 or len(content) < 60:
                continue
            valid.append({
                "type": etype,
                "summary": summary[:80],
                "content": content[:2000],
                "training_value": entry.get("training_value", "normal")
            })

        return valid

    except (urllib.error.URLError, json.JSONDecodeError, KeyError, IndexError, ValueError,
            ConnectionError, TimeoutError, OSError) as e:
        logger.warning("icarus: LLM extraction failed (%s) — falling back to legacy", type(e).__name__)
        return []


def _legacy_session_write(platform, scores):
    """Fallback: original truncated session write (pre-LLM behavior)."""
    plat = platform or "cli"
    parts = []

    first_user = next(
        (
            ex["user"] for ex in state.exchanges
            if len(ex.get("user", "").strip()) > 50
            and not _is_system_injection(ex.get("user", ""))
        ),
        None
    )
    if first_user:
        parts.append(f"## Task\n{first_user[:_TASK_MAX]}")

    for ex in state.exchanges:
        resp = ex.get("assistant", "")
        if state.DECISION_RE.search(resp) and len(resp) > 100:
            parts.append(f"## Decision\n{resp[:500]}")
            break

    substantive = [ex for ex in state.exchanges if len(ex.get("assistant", "").strip()) > 100]
    if substantive:
        parts.append(f"## Result\n{substantive[-1]['assistant'][:_RESULT_MAX]}")

    content = "\n\n".join(parts) if parts else state.exchanges[-1].get("assistant", "")[:500]

    if substantive:
        result_text = substantive[-1]['assistant']
    else:
        result_text = content
    summary = re.sub(r"\s+", " ", result_text.replace("\n", " ")).strip()[:80]
    summary = re.sub(r"-{2,}", "—", summary)  # sanitize: prevent YAML frontmatter breakage

    if scores["total"] >= 0.6:
        tv = "high"
    elif scores["total"] >= 0.3:
        tv = "normal"
    else:
        tv = "low"

    state.write_entry("session", content, summary, platform=plat,
                     training_value=tv, status="completed")


def on_session_end(session_id="", platform="", completed=False, **kwargs):
    """Score session, extract entries via LLM, fall back to legacy truncation."""
    creative = state.load_creative()
    state.write_memory_file(creative)

    if not state.exchanges:
        return

    scores = state.score_session()
    if scores["total"] < 0.2:
        return

    plat = platform or "cli"

    # ── LLM extraction (primary) ──
    transcript = _build_transcript(state.exchanges)
    entries = _llm_extract_entries(transcript)

    if entries:
        for entry in entries:
            state.write_entry(
                entry["type"],
                entry["content"],
                entry["summary"],
                platform=plat,
                training_value=entry.get("training_value", "normal"),
                status="completed"
            )
        logger.info("icarus: LLM extracted %d entries from session", len(entries))
    else:
        # ── Legacy fallback ──
        logger.info("icarus: LLM extraction produced nothing — using legacy truncation")
        _legacy_session_write(platform, scores)
