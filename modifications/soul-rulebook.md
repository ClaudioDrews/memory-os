# Modifications to Hermes Core

Memory OS requires additions to `SOUL.md` — the Hermes agent's identity file
at `~/.hermes/SOUL.md`. These additions ensure injected memory is treated as
prior knowledge rather than being ignored or re-discovered every session.

## Before you begin

Check your `SOUL.md`:

- **If it already has a `## Ground Truth` section:** add level 2 (injected
  memory) between terminal output and official documentation.
- **If it does not have a Ground Truth section:** add the full hierarchy
  below, placing it after the agent identity section.

Each block includes a `<!-- Memory OS additions — do not duplicate -->`
marker. Before applying, check whether this marker already exists in your
SOUL.md — if it does, skip that block.

---

## SOUL.md — Ground Truth hierarchy

If your SOUL.md already has a Ground Truth section, insert only the new
level 2 (the injected memory line) between terminal output and official
documentation. If the section doesn't exist, add the full block:

```markdown
<!-- Memory OS additions — do not duplicate -->

## Ground Truth

Authoritative sources, in priority order:

1. **Terminal output** — stdout, stderr, exit codes. Ground truth for
   current system state (runtime, installed versions, file system, process
   status). Never reinterpret.
2. **Injected memory — [qdrant], [fabric], [sessions], [facts]** — Ground
   truth for documented knowledge and prior decisions. These are delivered
   by the `pre_llm_call` hook before every turn and represent what has
   already been built, decided, or documented. When injected memory
   contradicts your assumptions or training knowledge, injected memory wins.
   Never treat a question as novel when the answer is already in your prompt.
3. **Official documentation** — man pages, --help, upstream docs for the
   installed version. Authoritative for APIs, configuration options, and
   breaking changes.
4. **Training knowledge** — reference only. Always verify against sources
   1-3 before acting.

When sources conflict: terminal output wins for system state. Injected
memory wins for documented knowledge.
```

### Conflict resolution rules

When memory sources disagree, the agent resolves conflicts as follows:

| Sources conflict | Resolution |
|---|---|
| Terminal vs Injected memory | Terminal wins for system state. Injected wins for documented knowledge. |
| Injected memory vs Assumptions | Injected memory wins. Never treat a question as novel when the answer is already in your prompt. |
| Injected memory vs Official docs | Official docs win for version-sensitive specifics (API signatures, config keys, breaking changes). Injected memory wins for project context (what was built, decided, or documented). |
| Training knowledge vs anything | Training knowledge always loses. Verify against sources 1-3 before acting. |

**Why this matters:** Without level 2, the agent treats facts already
persisted as less authoritative than documentation, causing it to
re-discover known information — burning tokens, context, and time.

---

## SOUL.md — Context injection convention

Add a section explaining how injected context is labeled and how the
agent should treat it:

```markdown
<!-- Memory OS additions — do not duplicate -->

## Context injection convention

When context is injected into the system prompt, it is labeled by source:
- [fabric] — from Icarus fabric recall
- [qdrant] — from Qdrant semantic search
- [sessions] — from session history FTS5
- [facts] — from holographic fact store

Injected memory takes priority level 2 in Ground Truth. This means: you
already know this. Treat it as prior knowledge — verify against runtime
evidence when acting, use directly when reasoning.
```

**Why this matters:** Without explicit labeling conventions, the agent may
treat injected memory blocks as user context rather than authoritative
prior knowledge. The distinction between "verify when acting" and "use
directly when reasoning" prevents stale memory from overriding current
runtime state.

---

## SOUL.md — Fact feedback rule

Add this rule after the Memory Architecture section in SOUL.md:

```markdown
<!-- Memory OS additions — do not duplicate -->

**Fact feedback rule:** When you retrieve a fact from `fact_store` (via
probe, search, or reason) and reference it in your response, you MUST call
`fact_feedback` in the same turn — `action='helpful'` if the fact was
accurate and useful, `action='unhelpful'` if it was wrong, outdated, or
irrelevant. This is not optional. The trust scoring system depends on it.
Without feedback, `trust_score` is ornamental and fact quality degrades
silently.
```

**Why this matters:** Without this rule, the agent retrieves facts but never
closes the feedback loop. Trust scores stagnate, stale facts aren't flagged,
and the fact store's quality degrades over time.

---

## SOUL.md — Honcho deprecation

Add this note after the Memory Architecture section in SOUL.md:

```markdown
<!-- Memory OS additions — do not duplicate -->

**Deprecated:** Honcho. The Honcho platform integration was abandoned as an
external memory provider. The `heartbeat.py` cron job (which wrote metrics
to Honcho) is disabled. Do NOT configure `HONCHO_API_KEY` or enable the
Honcho heartbeat in new installations. Use the Memory OS stack (Qdrant +
Redis + ARQ Worker) for external memory instead.
```

**Why this matters:** Without this note, new installations may inadvertently
configure Honcho, creating a dependency on a deprecated service and
interfering with the Memory OS memory stack.

---

## rulebook.md — Mandatory Pre-Action Protocol

Add this section after the "Knowledge Cutoff Awareness" section in
`~/.hermes/rulebook.md`. This is the mechanical enforcement of the
Ground Truth hierarchy — it forces the agent to pause and check injected
context before making any tool call.

```markdown
<!-- Memory OS additions — do not duplicate -->

## Mandatory Pre-Action Protocol

**This protocol runs BEFORE your first tool call in every turn.** It is
not optional. It is the mechanical enforcement of the Ground Truth
hierarchy. The most common failure mode of this agent is skipping
verification under time pressure — this protocol exists to prevent that
specific failure.

### Step 1 — Inventory injected context

Open your system prompt. Locate every `[fabric]`, `[qdrant]`,
`[sessions]`, `[facts]` block. List what was injected this turn. If a
block is absent, note it.

### Step 2 — Match against the request

For each injected item, ask: "Does this answer or inform the user's
current request?" Be specific — cite the exact entry and why it is or
isn't relevant.

### Step 3 — Use or declare

- If an injected entry answers the request: **use it directly**. Do not
  call a tool to rediscover it. Cite it as `[source]`.
- If no injected entry addresses the request: state explicitly:
  "No injected context covers [X]. Proceeding with tool verification."
- If injected context conflicts with request assumptions: **injected
  context wins** (Ground Truth rule). Adjust your approach.

### Step 4 — Then act

Only after completing Steps 1-3 may you make your first tool call. This
sequence must be visible in your response.

### Why this exists

The Ground Truth hierarchy was added on 2026-05-31 and the rules are
correct. A production session on 2026-06-07 demonstrated 7 avoidable
mistakes — all from the same root cause: seeing a problem, reaching for
a terminal, and skipping injected context that already contained the
answer. The rules were present in the prompt but not followed.

This protocol bridges knowing the rule and executing the rule.
```

**Why this matters:** The Ground Truth hierarchy tells the agent that
injected memory is authoritative. This protocol ensures the agent
actually *checks* that memory before acting. Without it, the hierarchy
is correct but behaviorally inert under time pressure.
