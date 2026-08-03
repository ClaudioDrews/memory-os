```diff
+                      .
+                     /|\
+                    / | \
+                   /  |  \
+                  /   |   \
+                 / ,--+--, \
+                /,'   |   ',\
!               //  ,--+--,  \\
!              //__/    |    \__\\
-                 \     |     /
-                  \  __|__  /
-                   \/     \/
!                    '.   .'
!                  ____'.'____
!                 /           \
+                /  I C A R U S  \
+               /                 \
+              '~~~~~~~~~~~~~~~~~~~'
```

> **Self-memory and replacement models for Hermes agents.**
>
> *Remember your work. Train your replacement.*

## What this is

Icarus is a **Hermes plugin**. It runs inside Hermes and gives agents shared memory, training data extraction, and a model replacement pipeline.

Icarus is **not** an Obsidian plugin. Obsidian is an optional viewer/editor for the markdown files Icarus writes. You don't need Obsidian to use Icarus.

## What this is not

- Not an orchestration framework
- Not an agent router
- Not a dashboard
- Not an Obsidian community plugin
- Not a standalone app

## Architecture

```
┌──────────────────────────────────────────────────────────┐
│  Hermes Agent                                            │
│  ├── Icarus plugin (this repo)                           │
│  │   ├── hooks: auto-capture decisions, inject context   │
│  │   ├── tools: recall, write, search, train, switch     │
│  │   └── scoring: session quality, export weighting      │
│  │                                                       │
│  │         writes/reads                                  │
│  │            │                                          │
│  ▼            ▼                                          │
│  ~/my-vault/icarus/          (FABRIC_DIR)                │
│  ├── agent-decision-chose-fastify-abc1.md                │
│  ├── agent-review-rate-limiter-race-d4e2.md              │
│  ├── daily/2026-04-01.md     (Obsidian daily notes)      │
│  └── cold/                   (archived entries)          │
│                                                          │
│  ~/my-vault/                 (OBSIDIAN_VAULT_PATH)       │
│  └── .obsidian/app.json     (vault config)               │
│                                                          │
│  export-training.py ──► together.jsonl ──► Together AI   │
│                              │                           │
│                              ▼                           │
│                     fine-tuned replacement model          │
└──────────────────────────────────────────────────────────┘
```

## 2-minute quickstart

### 1. Install the plugin

```bash
git clone https://github.com/esaradev/icarus-plugin.git
mkdir -p ~/.hermes/plugins/icarus
cp -r icarus-plugin/* ~/.hermes/plugins/icarus/
```

### 2. Set environment variables

Add to your Hermes profile `.env` (e.g. `~/.hermes/.env`):

```bash
# required: where Icarus writes notes
FABRIC_DIR=~/Documents/my-vault/icarus

# optional: enable Obsidian wikilinks and daily notes
ICARUS_OBSIDIAN=1

# optional: vault root (if icarus notes are a subfolder)
OBSIDIAN_VAULT_PATH=~/Documents/my-vault

# optional: for training/eval tools
TOGETHER_API_KEY=tok-...
```

### 3. Start Hermes and verify

```bash
hermes chat
```

Type `/plugins` to verify:

```
Plugins (1):
  ✓ icarus v0.3.0 (16 tools, 4 hooks)
```

### 4. Initialize Obsidian (optional)

Inside your Hermes chat, say:

> Set up Obsidian for my notes

The agent will call `fabric_init_obsidian`, which creates `.obsidian/app.json` at your vault root and `daily/` inside your notes directory.

### 5. Write a test note and verify

Inside Hermes:

> Write a fabric note about testing the setup

Then open your vault in Obsidian. You should see:
- A new `.md` file in your notes directory with a readable title
- A daily note at `daily/2026-04-01.md` with a wikilink to it
- YAML frontmatter visible in the note

## What Icarus adds to Hermes

Hermes already has per-instance memory and a capable runtime. Icarus adds:

- **Cross-instance shared memory** -- agents on different profiles read each other's work through a shared `FABRIC_DIR`
- **Decision-quality tagging** -- entries carry `training_value` (high/normal/low) so noise doesn't pollute training data
- **Training data extraction** -- fabric entries become fine-tuning pairs with quality filtering and pair weighting
- **Model replacement pipeline** -- fine-tune a cheaper model from your agent's own history, eval it, switch to it

## Tools

### Memory

| Tool | What it does |
|------|-------------|
| `fabric_recall` | Ranked retrieval from shared memory |
| `fabric_write` | Write entries with linking, evidence, and handoff fields |
| `fabric_search` | Keyword grep across all entries |
| `fabric_pending` | Show work assigned to this agent |
| `fabric_curate` | Set training value (high/normal/low) on an entry |

### Training

| Tool | What it does |
|------|-------------|
| `fabric_export` | Export training pairs. Modes: high-precision, normal, high-volume |
| `fabric_train` | Start fine-tune, auto-selects best quality mode with enough pairs |
| `fabric_train_status` | Check job progress, updates model registry |

### Replacement models

| Tool | What it does |
|------|-------------|
| `fabric_models` | List all trained models with eval scores |
| `fabric_eval` | Compare candidate vs base model on fabric-derived prompts |
| `fabric_switch_model` | Activate a replacement model if eval passes threshold |
| `fabric_rollback_model` | Emergency rollback to previous model |

### Operational

| Tool | What it does |
|------|-------------|
| `fabric_brief` | Daily brief: pending work, recent activity, suggested action |
| `fabric_telemetry` | Recall/usage stats: what gets recalled, what gets used |
| `fabric_report` | Corpus health: entries by type, training value, trainable estimate |
| `fabric_init_obsidian` | One-time Obsidian vault setup (Hermes tool, not a shell command) |

## Hooks

4 automatic hooks fire without the agent calling anything:

- **on_session_start** -- loads SOUL, pending handoffs, recent context
- **pre_llm_call** -- injects relevant memories when the topic changes
- **post_llm_call** -- captures high-value decisions (decision + outcome + substantial user request)
- **on_session_end** -- scores session quality, writes structured note if threshold met

## Obsidian setup

Icarus is a **Hermes plugin**, not an Obsidian plugin. Obsidian just reads the markdown files.

**How it works:**
- `FABRIC_DIR` is where Icarus writes `.md` files (your notes directory)
- `OBSIDIAN_VAULT_PATH` is where `.obsidian/` lives (your vault root)
- `ICARUS_OBSIDIAN=1` enables wikilinks in note bodies and daily note linking
- `fabric_init_obsidian` is a Hermes tool -- call it from inside Hermes, not from the terminal

**Two setups:**

Dedicated vault (Icarus IS the vault):
```
FABRIC_DIR=~/icarus-vault
# OBSIDIAN_VAULT_PATH not needed
```

Subfolder in existing vault:
```
FABRIC_DIR=~/my-vault/icarus-notes
OBSIDIAN_VAULT_PATH=~/my-vault
```

## Builder -> reviewer -> fix

```
# builder finishes work, hands off
fabric_write(type="code-session", summary="rate limiter ready",
             status="open", assigned_to="daedalus")

# reviewer sees it at session start, writes linked review
fabric_write(type="review", summary="found race condition",
             review_of="icarus:a3f29b01")

# builder sees the review, writes linked fix
fabric_write(type="code-session", summary="fixed race condition",
             revises="icarus:a3f29b01")
```

## Memory -> training -> replacement model

```
1. Work normally. The plugin captures decisions and completions automatically.

2. Check readiness:
   fabric_export(mode="high-precision")

3. Fine-tune:
   fabric_train(suffix="my-agent-v2")

4. Check progress:
   fabric_train_status()

5. Evaluate:
   fabric_eval(candidate_model="user/my-agent-v2-abc123")

6. Switch:
   fabric_switch_model(model_id="user/my-agent-v2-abc123")
```

## Training value

Entries carry a `training_value` field: `high`, `normal`, or `low`.

- **high** -- decisions with outcomes, completed reviews, successful fixes
- **normal** -- default for most entries
- **low** -- generic session summaries, conversational exchanges

Export modes:
- `high-precision` -- only grounded entries: high-value, verified, linked reviews, structured sessions, or completed entries with evidence
- `normal` -- excludes low-value and skips noisy unstructured session notes unless grounded
- `high-volume` -- everything

## Profiles (Hermes v0.6.0+)

Hermes supports multiple profiles, and Memory OS is profile-aware.  When a
non-default profile is active, Hermes sets `HERMES_HOME` to
`<root>/profiles/<name>` (e.g. `~/.hermes/profiles/coder`), and Memory OS
resolves every Hermes-owned path from it — fabric, `state.db`,
`memory_store.db`, logs, wiki state, DLQ, SOUL.md.  Each profile gets its
own memory automatically.

```bash
hermes profile create coder
hermes profile create reviewer --clone

# Install Memory OS / Icarus into each profile
bash setup.sh --profile coder
bash setup.sh --profile reviewer

hermes -p coder chat
```

By default each profile writes to its own `FABRIC_DIR`
(`<profile-home>/fabric`), so coder and reviewer memories stay isolated.

To deliberately **share** a fabric between profiles (e.g. the reviewer reads
the coder's work), set `FABRIC_DIR` explicitly to the same absolute path in
each profile's `.env`:

```bash
# ~/.hermes/profiles/coder/.env  and  ~/.hermes/profiles/reviewer/.env
FABRIC_DIR=/home/you/vault/fabric
```

## Fallback models

After switching to a replacement model, set the original as fallback in `config.yaml`:

```yaml
model: user/my-agent-v2-abc123
fallback_model:
  provider: openrouter
  model: anthropic/claude-sonnet-4
```

## Troubleshooting

**"tool not found" when calling fabric_write or fabric_recall**
- Run `/plugins` in Hermes. If Icarus isn't listed, the plugin isn't installed in the right directory.
- Check: `ls ~/.hermes/plugins/icarus/__init__.py` (global) or `ls ~/.hermes-YOUR_PROFILE/plugins/icarus/__init__.py` (profile-specific Hermes home).
- The plugin needs `__init__.py`, `plugin.yaml`, and all `.py` files in the same directory.
- If you copied the repo twice, make sure you do **not** have a nested path like `~/.hermes/plugins/icarus/icarus-plugin/__init__.py`.

**Notes not showing in Obsidian**
- Check `FABRIC_DIR` points to a directory inside your Obsidian vault.
- Open the vault root (not the notes subdirectory) in Obsidian.
- If you set `OBSIDIAN_VAULT_PATH`, make sure `FABRIC_DIR` is inside it.

**"I pointed FABRIC_DIR at the wrong directory"**
- Change `FABRIC_DIR` in your `.env` and restart Hermes. Existing notes stay where they were. Move them manually if needed.

**".obsidian ended up in the wrong place"**
- Delete the misplaced `.obsidian/` directory.
- Set `OBSIDIAN_VAULT_PATH` to your actual vault root.
- Call `fabric_init_obsidian` again inside Hermes.

**"I expected an Obsidian plugin"**
- Icarus is a Hermes plugin, not an Obsidian community plugin. There is nothing to install in Obsidian. Obsidian reads the markdown files directly -- no plugin needed.

**Wikilinks not appearing in notes**
- Set `ICARUS_OBSIDIAN=1` in your `.env` and restart Hermes. Links are only added when this flag is set.

## Validation

After setup, verify everything works:

```
1. In Hermes: "write a test note about validating the setup"
2. Check: ls $FABRIC_DIR/*.md (should show a new file)
3. Check: ls $FABRIC_DIR/daily/ (should show today's date)
4. Open vault in Obsidian: note should appear with frontmatter
5. In Hermes: fabric_brief() (should show the note in recent work)
```

## Smoke test

```bash
bash scripts/smoke-handoff.sh
bash scripts/test-plugin.sh
```

## Requirements

- [Hermes](https://github.com/NousResearch/hermes-agent) v0.6.0+
- Python 3.10+
- `TOGETHER_API_KEY` in `.env` (for training/eval tools)
- `FABRIC_DIR` set in `.env` (defaults to `~/fabric/`)

## Files

```
__init__.py           registration (16 tools, 4 hooks)
plugin.yaml           manifest
schemas.py            tool schemas (what the LLM sees)
tools.py              tool handlers
hooks.py              lifecycle hooks
state.py              fabric I/O, session scoring, model registry
obsidian.py           opt-in Obsidian formatting
fabric-retrieve.py    ranked retrieval with scoring
export-training.py    training pair extraction with quality filtering
scripts/
  eval-replacement.py model comparison eval
  smoke-handoff.sh    end-to-end handoff proof
  test-plugin.sh      66-test fixture suite
```

## License

MIT
