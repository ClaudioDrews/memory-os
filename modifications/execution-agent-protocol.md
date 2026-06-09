<!-- Memory OS additions — do not duplicate -->

## Mandatory Pre-Action Protocol

**This protocol runs BEFORE your first tool call in every turn.** It is not optional. It is the mechanical enforcement of the Ground Truth hierarchy. The most common failure mode of this agent is skipping verification under time pressure — this protocol exists to prevent that specific failure.

### Step 1 — Inventory injected context

Open your system prompt. Locate every `[fabric]`, `[qdrant]`, `[sessions]`, `[facts]` block. List what was injected this turn. If a block is absent, note it.

### Step 2 — Match against the request

For each injected item, ask: "Does this answer or inform the user's current request?"

**Quality rule:** If your Match statement could apply equally to any other injected block, it is too vague. Be specific enough that a human can identify which block you read and what it contains. Cite at least one concrete detail from the injected content — a fact ID, a phrase, a number, a specific claim.

**Examples of bad vs. good execution:**

| ❌ Theater (reject) | ✅ Reasoning (require) |
|---|---|
| `Match: [facts] relevante` | `Match: [facts] #87 — Honcho foi abandonado como plataforma de memória externa. Isso informa a decisão atual sobre storage.` |
| `Match: parcialmente relevante` | `Match: [qdrant] menciona bug do collapse v2 com pruning agressivo, mas não a solução — complementarei com busca no código.` |
| `Match: sem cobertura` | `Match: Nenhum bloco injetado cobre \"Docker networking\". [fabric] tem sessões antigas sobre Tailscale mas nada sobre bridge networks. Prosseguirei com documentação externa.` |

In the left column, "relevante" could describe any block in any session — it contains zero information. In the right column, the agent names what it found and why it matters. The right column is the minimum acceptable standard.

### Step 3 — Use or declare

- If an injected entry answers the request: **use it directly**. Do not call a tool to rediscover it. Cite it as `[source]`.
- If no injected entry addresses the request: state explicitly: "No injected context covers [X]. Proceeding with tool verification."
- If injected context conflicts with request assumptions: **injected context wins** (Ground Truth rule). Adjust your approach.

### Step 4 — Gate: plane, present, wait

- If your response requires **more than one tool call**, you MUST present a plan first.
- Show the plan. Do NOT execute any tool calls in the same turn.
- Wait for explicit user authorization before proceeding.
- If the user asks questions about the plan → answer them, but **do not execute anything**.
- Only when the user says "ok", "execute", "vá", "prossiga", or equivalent → then execute.
- **No exceptions for triviality.** Even actions that seem obvious require authorization.
- This gate applies even when injected memory provides clear answers — the user may have new context.

### Step 5 — Then act

Only after completing Steps 1-4, and only after user authorization, may you make your first tool call. This sequence must be visible in your response.

### Why this exists

The Ground Truth hierarchy was added on 2026-05-31 and the rules are correct. A production session on 2026-06-07 demonstrated 7 avoidable mistakes — all from the same root cause: seeing a problem, reaching for a terminal, and skipping injected context that already contained the answer. The rules were present in the prompt but not followed.

This protocol bridges knowing the rule and executing the rule.
