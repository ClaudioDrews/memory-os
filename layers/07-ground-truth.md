# Layer 7 — Ground Truth Hierarchy

> **Type:** Identity-layer fix (SOUL.md + rulebook.md)
> **Why it exists:** Context injection is not enough — the agent must be *instructed* to treat injected memory as authoritative.
> **Discovered:** 2026-05-31

## The problem

Memory OS successfully injects context from all four sources (Fabric + Qdrant + Sessions + Facts) into every prompt. You can see it in the system preamble: `[qdrant]`, `[fabric]`, `[sessions]`, `[facts]` blocks are right there.

**But the agent ignores them.**

Symptoms:
- Agent runs `search_files`, `read_file`, `session_search` to rediscover information that `[qdrant]` already provided
- Treats every question as novel even when the answer is literally in the prompt
- Rediscovers projects, decisions, and constraints from scratch each session

## Root cause

Memory OS was injecting memory into the prompt, but the agent's **identity documents** (`SOUL.md` and `rulebook.md`) did not include injected memory in the Ground Truth hierarchy. Without an explicit rank, the injected context was implicitly treated as optional suggestion — below terminal output and official documentation.

The original hierarchy had only 3 levels:

```
1. Terminal output → Ground Truth
2. Official documentation → Authoritative
3. Training knowledge → Reference only
```

The injected memory (`[qdrant]`, `[fabric]`, `[sessions]`, `[facts]`) was **not listed at all**. No status = no authority.

## The fix

The hierarchy was expanded to 4 levels, with injected memory inserted as the second level:

```
1. Terminal output → Ground Truth for system state (runtime)
2. Injected memory [qdrant, fabric, sessions, facts] → Ground Truth for
   documented knowledge and prior decisions
3. Official documentation → Authoritative for APIs, configs, version-specifics
4. Training knowledge → Reference only; always verify against 1-3
```

### Conflict resolution

| Sources conflict | Winner |
|---|---|
| Terminal vs Injected memory | Terminal wins for system state. Injected memory wins for documented knowledge. |
| Injected memory vs Assumptions | **Injected memory wins.** Never treat a question as novel when the answer is already in your prompt. |
| Injected memory vs Official docs | Official docs win for version-sensitive specifics. Injected memory wins for project context. |
| Training knowledge vs anything | Training knowledge always loses. Verify against 1-3. |

### Files changed

| File | Change |
|------|--------|
| `~/.hermes/SOUL.md` | Ground Truth section expanded from 3 to 4 levels; added conflict rules |
| `~/.hermes/rulebook.md` | Added "Injected memory" row to Source of Truth table; added mandatory verification behavior |

### Key instruction added to SOUL.md

> *"When injected memory contradicts your assumptions, injected memory wins. Never treat a question as novel when the answer is already in your prompt."*

## Why this matters

The infrastructure layers (01-06) ensure memory is captured, stored, and injected. Layer 07 ensures the injected memory is **used**. Without it:

- Qdrant points are injected but the agent `curl`s the Qdrant API to verify them
- Fabric entries are injected but the agent calls `fabric_recall` to re-find them
- Session history is injected but the agent runs `session_search` to re-discover it
- Facts are injected but the agent probes `fact_store` to confirm them

Each rediscovery burns tokens, time, and model context. Layer 07 is what stops the waste.

## Verification

After applying this fix (updating SOUL.md and rulebook.md), the agent should:

1. Read injected `[qdrant]`, `[fabric]`, `[sessions]`, `[facts]` blocks before running any search/discovery tools
2. Not rediscover knowledge that is already in the prompt
3. Cite injected context directly instead of re-deriving it
4. Respect the conflict rules when sources disagree

A gateway restart is required after editing SOUL.md or rulebook.md for changes to take effect in new sessions:

```bash
systemctl --user restart hermes-gateway
```

## Related

- [Layer 4 — Fabric (injection mechanism)](04-icarus-fabric.md)
- [Layer 5 — Qdrant (vector source)](05-qdrant.md)
- [Layer 3 — Fact Store (structured facts)](03-fact-store.md)
- [Layer 2 — Sessions](02-sessions.md)

## 2026-06-07: The gap between knowing and executing

A production session revealed that the Ground Truth hierarchy alone is
not sufficient. The agent made 7 avoidable mistakes in sequence, all
sharing the same root cause: the injected context was present in the
prompt, the rules were loaded, but the agent defaulted to "resolve fast"
mode instead of "verify first."

Two discoveries emerged:

1. **Qdrant injection was silently broken.** The `hooks.py` import
   `from scripts.context_enhancer import ...` resolved to a path where
   the file never existed — the code lived under the Memory OS repo.
   The `except Exception: return []` pattern swallowed the
   `ModuleNotFoundError`, and `[qdrant]` was never injected. This had
   been broken since 2026-05-29 without detection.

2. **Behavioral inertia outpaces documentation.** The Ground Truth
   hierarchy was correctly documented and injected into the prompt.
   The agent read it every turn. It still defaulted to the faster
   operational mode.

### The fix

**Technical:** Added a symlink step to `setup.sh` (Phase 5b) so
`context_enhancer.py` is reachable from the import path.

**Behavioral:** Added the **Mandatory Pre-Action Protocol** to
`rulebook.md` — a 4-step mechanical sequence (inventory → match →
use/declare → act) that runs before every tool call. This protocol
bridges the gap between knowing the rule and executing it under
time pressure.

**Documentation updated:**
- `modifications/soul-rulebook.md` — new protocol section
- `modifications/execution-agent-protocol.md` — standalone protocol
  for automated application by setup.sh
- `setup.sh` — Phase 5b (symlink) + Phase 9 (auto-apply protocol)
