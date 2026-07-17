---
name: context-injection
description: "Configure and tune automatic context injection for Memory OS. Covers the Icarus pre_llm_call hook flow, 4 sources (Fabric, Qdrant, Sessions, Facts), threshold gating, per-session dedup, the social closer filter, multi-source deduplication, and injection troubleshooting."
version: 1.0.0
triggers:
  - Tuning context injection thresholds
  - Debugging why context is not being injected
  - Understanding how the 4 memory sources interact
  - Reducing token cost from context injection
  - Diagnosing duplicate context in system prompt
  - Configuring social closer detection
  - Improving recall relevance
---

# Context Injection — Multi-Source pre_llm_call Pipeline

## Overview

The Icarus plugin hooks into the agent's `pre_llm_call` lifecycle to inject relevant context before every LLM turn. Four sources are queried in parallel, each gated by relevance thresholds, and results are deduplicated across the session.

## Injection Flow

```
User message
    │
    ▼
pre_llm_call(user_message):
    ├── _is_social_close(message)?
    │     └── Yes → skip ALL search-based injection
    ├── fabric_recall(query) → [fabric] results
    ├── _search_qdrant(query) → [qdrant] results
    ├── _search_sessions(query) → [sessions] results
    └── _search_facts(query) → [facts] results (first turn only)
    │
    ▼
System prompt assembled with injected context
```

## Source Configuration

| Source | Method | Threshold | Top-K | Cost | Label |
|--------|--------|-----------|-------|------|-------|
| **Fabric** | `fabric_recall(query)` | overlap > 0.85 → skip | 3 | low (local markdown) | `[fabric]` |
| **Qdrant** | `_search_qdrant(query)` → context_enhancer | threshold ≥ 0.55 | 2 | high (API embedding) | `[qdrant]` |
| **Sessions** | FTS5 on state.db | tokens ≥ 4 chars | 2 | low (local SQLite) | `[sessions]` |
| **Facts** | FTS5 on memory_store.db | first turn only | 3 | low (local SQLite) | `[facts]` |

### Threshold tuning rationale

**Overlap gate (0.85):** Was set to 0.6 originally — this suppressed ALL injection from turn 2 onward in long single-topic sessions. The agent went blind to all sources midway through focused conversations. The fix: only suppress near-literal repetition (>0.85) and rely on per-source dedup instead.

**Qdrant threshold (0.55):** Was set to 0.72 — legitimate queries scored 0.57-0.63 and were silently filtered. 0.55 surfaces them; top_k stays low (2) to bound prompt size.

**Facts (first turn only):** Facts are stable, rarely change per session. Querying every turn wastes tokens. First-turn-only injection is the right tradeoff.

## Social Closer Gate

Trivial messages skip expensive search entirely:

```python
_SOCIAL_CLOSERS = frozenset({
    "ok", "thanks", "yes", "no", "👍", "👌", "✅", "done", "got it",
    "certo", "sim", "claro", "beleza"
})
```

Also blocks Qdrant/sessions/facts (NOT fabric — fabric has zero network cost) for:
- Messages shorter than 6 chars that are ASCII-only without technical markers (`://.@#$_?`)

**Why not `len > 20`:** Messages like "fix the BM25" or "probe Qdrant" are short but technically dense — exactly the queries that benefit most from semantic injection.

## Per-Session Deduplication

Module-level sets (`_injected_fabric`, `_injected_qdrant`, `_injected_sessions`) are reset in `on_session_start`.

**Dedup key:** Prefer the entry/point `id`; fall back to a stable content slice (e.g. `summary[:60]`). A too-short slice (`[:40]`) collides and produces duplicate lines.

**What this replaces:** The old approach used a single `overlap > 0.6` bail that killed ALL injection after turn 1. Now individual sources are deduplicated independently, so injection stays active across a long session without repeating the same content.

## Context Enhancer (Qdrant Pipeline)

The Qdrant search path uses `context_enhancer.py` — a standalone embedding + search pipeline:

```python
search_with_fallback(query, top_k=2, threshold=0.55):
    1. Embed via Qwen3-Embedding-8B (OpenRouter)
    2. try hybrid (dense + BM25 sparse) → RRF fusion
    3. except -> try dense-only
    4. except -> try lexical (markdown grep)
    5. except -> try SQLite keyword
    6. fail -> return empty
```

`context_enhancer.py` accepts any OpenAI-compatible endpoint through
`EMBEDDING_API_BASE`, with `EMBEDDING_API_KEY` for authenticated custom
providers. OpenRouter remains the default and continues to use
`OPENROUTER_API_KEY`. Query embedding is synchronous so retrieval completes
before the main LLM receives its prompt; timeout/retry behavior is controlled by
`EMBEDDING_REQUEST_TIMEOUT` and `EMBEDDING_REQUEST_RETRIES`.

## Injecting into System Prompt

Results are labeled by source:

```
[memory]
[fabric] relevant to your request:
  [2026-05-30] agent: Fixed context injection thresholds

[sessions] prior conversations on this topic:
  [2026-05-24] (untitled): …Discussion about memory thresholds…

[qdrant] semantically similar knowledge:
  [concept] Memory Architecture — Fallback cascade (hybrid → dense → lexical)
  [entity] Qdrant v1.17 — Named vectors required for hybrid search

[facts] structured facts:
  - Context injection thresholds: overlap > 0.85, Qdrant ≥ 0.55
```

Source labeling lets the agent understand where each piece of information came from and apply the correct priority level from Ground Truth hierarchy.

## Troubleshooting

### No context being injected
- Check `_is_social_close()` — trivial messages skip search
- Verify `OPENROUTER_API_KEY` is resolvable at import time (hooks.py reads at module level)
- Check gateway was restarted after any `.env` or `hooks.py` changes
- Verify the overlap threshold is 0.85 (not 0.6)

### Duplicate context appearing
- Check dedup key slice — if too short (`[:40]`), different entries may collide
- Verify `_injected_*` sets are reset in `on_session_start`

### Qdrant search returning empty but Qdrant has data
- Check embedding dimension mismatch (env vs collection)
- Check if threshold is too high (> 0.55 hides legitimate matches)
- Check if `OPENROUTER_API_KEY` was injected before context_enhancer import

### Fabric results stale or missing
- Verify `fabric_recall` is working (`fabric_brief()` shows entries)
- Check `FABRIC_DIR` points to a directory with `.md` files
- Check `ICARUS_EXTRACTION_MAX_TOKENS` is set to 4096 (1024 default truncates)

## Pitfalls

- **`ICARUS_EXTRACTION_MAX_TOKENS` is frozen at import time** — changing `.env` requires gateway restart. Value is read at `hooks.py:15` and cached in process memory.
- **DeepSeek + `response_format: json_object` = `content: null`** — use prompt-based JSON + `_parse_json_robust()` instead
- **FTS5 `snippet()` cannot be combined with `GROUP BY`** — fetch ranked rows, dedup in Python
- **`sessions.started_at` is a Unix timestamp (float)** — not an ISO string. Use `datetime.fromtimestamp(float(sa))`
- **Open DBs read-only** — `sqlite3.connect(f"file:{db}?mode=ro", uri=True)`. The gateway must never lock or mutate these files from a hook
- **FTS5 is lexical, not semantic** — queries in one language may not match content in another with different wording
- **Always restart gateway** (`hermes gateway restart`) after editing `hooks.py` — hooks are loaded at import, changes only apply to new sessions
