# Memory OS — Hermes Agent Memory Operating System

![Memory OS Banner](assets/banner.jpg)

> **Your agent finally stops forgetting.**  \
> Permanent memory. Local memory infrastructure. API-provider agnostic. Surgically token-efficient.

Seven memory layers. Automatic, intelligent context injection. Structured facts with trust scoring. A self-curating wiki pipeline. Semantic search across **every conversation you've ever had**.

Memory OS turns Hermes Agent into a real long-term collaborator — one that remembers your projects, your decisions, your reasoning, and brings exactly the right context back at exactly the right moment. Like talking to a colleague who was there for every session.

**Memory infrastructure runs entirely on your machine. Works with any LLM provider — OpenRouter, OpenAI, Anthropic, Ollama, or local models. No memory subscription. No vendor lock-in.**

---

## What's New in v0.2.0

**One-command install.** `curl -sSL https://raw.githubusercontent.com/ClaudioDrews/memory-os/main/setup.sh | bash` sets up the entire stack — Docker services, SQLite databases, Icarus plugin, environment — in one shot. The 10-step manual guide is now a fallback for troubleshooting.

**Community infrastructure.** Issue templates (bug report + feature request), PR checklist, and contributing guide. Project is ready for external contributors — and already has them.

**20+ fixes from systematic audit.** Community-driven review across setup, configuration, performance, and resilience. Highlights: provider-agnostic LLM extraction, O(1) path lookups, FTS5-powered session search, semantic dedup at scale, and idempotent database initialization.

**Installation verified on real hardware.** Smoke tests and ingestion tests ship with the repo. The automated installer has been tested end-to-end — including on modest machines where Docker build times exposed UX gaps that are now handled gracefully.

---

## The problem every serious Hermes user knows

You spend hours configuring the agent, teaching it your preferences, solving hard problems together — and in the next session it acts like it's meeting you for the first time.

- Repeating context at the start of every conversation
- Losing the thread of important decisions made weeks ago
- Structured facts — your stack, your projects, your patterns — with nowhere to live
- Every memory solution you've tried is either cloud-locked or too shallow to matter

After months of hitting these walls in production, I built something that actually works.

---

## What Memory OS is

Not just another plugin. A complete **memory operating system** — 7 layers working in concert, from flat files to a vector database, with surgical context injection, a knowledge pipeline that organizes itself, **and an explicit Ground Truth hierarchy that tells the agent to actually use the injected memory**.

Designed and refined by someone who ran headfirst into every limitation of stock Hermes and every existing memory solution.

**Requirements:** Hermes Agent + Docker (Qdrant + Redis + ARQ Worker) + Python 3.11+.  
Compatible with any LLM provider Hermes supports — OpenRouter, OpenAI, Anthropic, Ollama, and more.

---

## Architecture: 7 memory layers

```
┌──────────────────────────────────────────────────────────────────┐
│  LAYER 1 · WORKSPACE                                              │
│  MEMORY.md · USER.md · CREATIVE.md                               │
│  → Injected into the system prompt every single turn             │
├──────────────────────────────────────────────────────────────────┤
│  LAYER 2 · SESSIONS                                               │
│  state.db (SQLite + FTS5)                                         │
│  → Full-text search across your entire conversation history       │
├──────────────────────────────────────────────────────────────────┤
│  LAYER 3 · STRUCTURED FACTS                                       │
│  memory_store.db (SQLite + HRR + FTS5 + trust scoring)            │
│  → Durable facts with entity resolution and an automatic          │
│    feedback loop that trains trust scores over time               │
├──────────────────────────────────────────────────────────────────┤
│  LAYER 4 · FABRIC (CROSS-SESSION)                                 │
│  Icarus Plugin (heavily forked)                                   │
│  → LLM-powered session extraction + multi-source injection        │
│  → 16 tools: fabric_recall, fabric_write, fabric_brief, etc.      │
├──────────────────────────────────────────────────────────────────┤
│  LAYER 5 · VECTOR DATABASE                                        │
│  Qdrant (4096d Cosine + BM25 sparse)                              │
│  → 4-level fallback: hybrid → dense → lexical → SQLite            │
│  → Weekly decay scanner + semantic dedup (cosine >0.92 → merge)  │
├──────────────────────────────────────────────────────────────────┤
│  LAYER 6 · LLM WIKI                                               │
│  Auto-curated vault: concepts/ · entities/ · comparisons/         │
│  → Continuously ingested into Qdrant via wiki-continuous-ingest   │
├──────────────────────────────────────────────────────────────────┤
│  ⚡ LAYER 7 · GROUND TRUTH HIERARCHY (identity layer)              │
│  SOUL.md · rulebook.md                                             │
│  → Tells the agent that injected memory is authoritative           │
│  → Without this, layers 2-6 deliver context the agent ignores     │
└──────────────────────────────────────────────────────────────────┘
```

**How it flows:**

`pre_llm_call` → surgical recall from all four sources (Fabric + Qdrant + Sessions + Facts)

**But recall is not enough.** The agent must be explicitly instructed to treat this injected context as authoritative. That's what [Layer 7](layers/07-ground-truth.md) provides — without it, the agent rediscovers knowledge that's already in the prompt.

`post_llm_call` + `on_session_end` → automatic learning extraction and capture

Each source is gated by relevance thresholds. Per-session deduplication prevents the same context from appearing twice. A social-closer filter skips trivial messages entirely. No padding. No firehose. The LLM gets exactly what it needs — nothing more.

---

## Why Layer 7 is the most important layer

Layers 1-6 ensure memory is **captured, stored, and injected**. Layer 7 ensures the injected memory is **used**.

Without the Ground Truth hierarchy:
- Qdrant points are injected but the agent calls the Qdrant API to verify them
- Fabric entries are injected but the agent runs `fabric_recall` to re-find them  
- Session history is injected but the agent runs `session_search` to re-discover it
- Facts are injected but the agent probes `fact_store` to confirm them

The result: **memory-zero behavior** despite perfect injection. Every rediscovery burns tokens, context, and time.

→ **[Read Layer 7: Ground Truth Hierarchy](layers/07-ground-truth.md)** — the critical fix.

---

## Memory OS vs. stock Hermes

| Aspect | Stock Hermes | Memory OS |
|---|---|---|
| Workspace memory | MEMORY.md + USER.md | + CREATIVE.md + intelligent injection |
| Session memory | Basic state.db | + FTS5 full-text search + session injection |
| Structured facts | Not present | Fact store + trust scoring + feedback loop |
| Cross-session recall | Limited | Fabric fork + multi-source injection |
| Vector search | Not present | Qdrant hybrid + 4-level fallback cascade |
| Cleanup and deduplication | Not present | Decay scanner + semantic dedup + archival |
| Knowledge pipeline | Not present | Self-curating LLM Wiki |
| **Ground Truth hierarchy** | **Not present** | **Injected memory ranked as authoritative; agent must use context provided** |
| Token efficiency | — | Surgical: gated retrieval + per-session dedup + no wasted rediscovery |
| Infrastructure | — | Local memory stack (Qdrant + Redis + ARQ) + any LLM provider |

---

## Why not mem0, Zep, Letta, or other providers?

Because almost every modern memory solution is **cloud-first**. If you want real, private memory infrastructure running on your own machine — with no cloud memory subscription, full provider flexibility, and no data leaving your local stack — none of them deliver what Memory OS delivers.

| | Memory OS | mem0 | Zep | Letta |
|---|---|---|---|---|
| Local memory infrastructure | ✓ | ✗ | ✗ | ✗ |
| No memory subscription | ✓ | ✗ | ✗ | ✗ |
| Provider agnostic (OpenRouter, Ollama…) | ✓ | Partial | Partial | Partial |
| Hermes-native | ✓ | ✗ | ✗ | ✗ |
| Structured facts + trust scores | ✓ | Partial | ✗ | ✗ |
| Self-curating wiki | ✓ | ✗ | ✗ | ✗ |
| Intelligent decay + archival | ✓ | ✗ | ✗ | ✗ |
| **Ground Truth hierarchy** | **✓** | **✗** | **✗** | **✗** |

---

## Included components

- **Icarus Plugin (heavily modified fork)** — bundled in `icarus/`  
  The upstream [esaradev/icarus-plugin](https://github.com/esaradev/icarus-plugin) is the base, but this fork is not upstream-compatible. Key additions: LLM-powered session extraction (replaces `text[:500]` truncation), multi-source injection (Qdrant + sessions + facts — upstream is fabric only), CREATIVE.md isolation (fixes `§` delimiter corruption from dual-writer conflict), backtick sanitization, system injection filter, and social closer detection.

- **Vault Curator v3** — [ClaudioDrews/vault-curator](https://github.com/ClaudioDrews/vault-curator)  
  Frontmatter enrichment, semantic linking, and MOC index generation for the wiki layer.

---

## Who this is for

For people who take Hermes Agent seriously.  
For people who want an agent that **actually evolves** over time — one that doesn't need the world re-explained every session.  
For people who value clean engineering, extreme efficiency, and solutions that hold up in real local production.

If you're like me — tired of amnesiac agents — Memory OS was built for you.

---

**Want to see the agent remember for real?**  
Clone it, run it, feel the difference.

→ [Quick install](setup.sh) · [Manual guide](setup/install.md) · [Layer deep-dives](layers/) · [Infrastructure docs](infrastructure/architecture.md) · [Operational skills](skills/) · [License](LICENSE)

MIT License · Built with obsession by someone who runs Hermes every single day.