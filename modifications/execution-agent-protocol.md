<!-- Memory OS additions — do not duplicate -->

## Mandatory Pre-Action Protocol

**This protocol runs BEFORE your first tool call in every turn.** It is not optional. It is the mechanical enforcement of the Ground Truth hierarchy. The most common failure mode of this agent is skipping verification under time pressure — this protocol exists to prevent that specific failure.

### Step 1 — Inventory injected context

Open your system prompt. Locate every `[fabric]`, `[qdrant]`, `[sessions]`, `[facts]` block. List what was injected this turn. If a block is absent, note it.

### Step 2 — Match against the request

For each injected item, ask: "Does this answer or inform the user's current request?" Be specific — cite the exact entry and why it is or isn't relevant.

### Step 3 — Use or declare

- If an injected entry answers the request: **use it directly**. Do not call a tool to rediscover it. Cite it as `[source]`.
- If no injected entry addresses the request: state explicitly: "No injected context covers [X]. Proceeding with tool verification."
- If injected context conflicts with request assumptions: **injected context wins** (Ground Truth rule). Adjust your approach.

### Step 4 — Then act

Only after completing Steps 1-3 may you make your first tool call. This sequence must be visible in your response.

### Why this exists

The Ground Truth hierarchy was added on 2026-05-31 and the rules are correct. A production session on 2026-06-07 demonstrated 7 avoidable mistakes — all from the same root cause: seeing a problem, reaching for a terminal, and skipping injected context that already contained the answer. The rules were present in the prompt but not followed.

This protocol bridges knowing the rule and executing the rule.
