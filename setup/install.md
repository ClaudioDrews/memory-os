# Setup Guide

> Step-by-step installation of the Memory OS stack. Assumes Hermes Agent is already installed and configured.

## Prerequisites

- Hermes Agent 0.14.0+ (tested on 0.15.2)
- Python 3.11+
- Docker 24.0+
- OpenRouter API key **only if using OpenRouter as embedding backend** (Ollama/vLLM/llama.cpp local providers do not require a key — see [Layer 5: Qdrant](../layers/05-qdrant.md))
- 16 GB RAM recommended (8 GB minimum)

## Installation

### 1. Icarus Plugin (bundled)

```bash
# Copy the bundled Icarus fork into the Hermes plugins directory
cp -r icarus/ ~/.hermes/plugins/icarus/
```

### 2. Enable Icarus in Hermes Config

Icarus must be registered as an enabled plugin. Edit `~/.hermes/config.yaml`:

```yaml
enabled:
  - hermes-achievements       # optional
  - icarus                    # required — activates fabric tools + context injection hooks
```

Then restart the gateway:

```bash
hermes gateway restart
```

Verify the plugin loaded:

```bash
hermes status
# → Should show: icarus v0.3.0 (16 tools, 4 hooks)
```

### 3. Docker Infrastructure

```bash
# Copy docker-compose.yml from this repository
cp docker/docker-compose.yml ~/memory-os/
cd ~/memory-os

# Create .env with required variables
cat > .env << EOF
# Required only for OpenRouter embedding backend; safe to leave empty for local providers
OPENROUTER_API_KEY=sk-or-...
REDIS_PASSWORD=$(openssl rand -hex 16)
EMBEDDING_DIMS=4096
COLLECTION_NAME=knowledge_base
EOF

docker compose up -d
```

Verify:
```bash
curl -s http://localhost:6333/healthz  # → {"title":"ok","version":"1.17.1"}
redis-cli -a "$REDIS_PASSWORD" ping    # → PONG
```

### 4. Environment Variables

Add to your Hermes profile `.env` (e.g. `~/.hermes/.env`):

```bash
# Required
FABRIC_DIR=/home/your-user/vault/fabric

# Required only when using OpenRouter as embedding backend
OPENROUTER_API_KEY=sk-or-...

# Strongly recommended
ICARUS_EXTRACTION_MAX_TOKENS=4096
ICARUS_EXTRACTION_MODEL=deepseek/deepseek-v4-flash
EMBEDDING_DIMS=4096

# Optional — Embedding backend (defaults to OpenRouter)
# EMBEDDING_API_BASE=https://openrouter.ai/api/v1
# EMBEDDING_MODEL=qwen/qwen3-embedding-8b

# Optional
ICARUS_OBSIDIAN=1
ICARUS_RESULT_MAX_CHARS=500
ICARUS_TASK_MAX_CHARS=300
```

**⚠️ Use absolute paths.** The Hermes gateway runs as a systemd service — `~` is not expanded. Always use `/home/your-user/...`.

### 5. Core File Modifications

Apply the changes documented in [modifications/soul-rulebook.md](../modifications/soul-rulebook.md):

- Add Ground Truth level 2 (injected memory) to `SOUL.md`
- Add memory architecture documentation to `rulebook.md`
- Add context injection convention to `SOUL.md`

These modifications ensure the agent trusts its injected memory as authoritative.

### 6. Wiki Setup

```bash
mkdir -p $VAULT_PATH/wiki/{raw,concepts,entities,comparisons,_meta,_archive}
# Copy SCHEMA.md template, create initial index.md and log.md
```

The wiki starts empty. Add source documents to `raw/` and the wiki-agent cronjob will begin extracting structured pages.

### 7. Cronjobs

Add to crontab (`crontab -e`):

```cron
# Wiki ingestion — keeps Qdrant in sync
0 * * * *   /usr/bin/python3 /path/to/scripts/wiki_continuous_ingest.py

# Qdrant maintenance
0 3 * * 0   /usr/bin/python3 /path/to/scripts/decay_scanner.py

# Dead letter queue monitoring
0 */6 * * * /usr/bin/python3 /path/to/scripts/dlq_manager.py

# Semantic dedup (first Sunday of month)
0 3 * * 0   [ $(date +\%d) -le 7 ] && /usr/bin/python3 /path/to/scripts/semantic_dedup.py
```

### 8. Gateway Restart

```bash
hermes gateway restart
```

Changes to `.env`, `SOUL.md`, `rulebook.md`, and Icarus plugin code only take effect after restart.

### 9. Verify

Inside Hermes chat:

```
/plugins
# → Should show: icarus v0.3.0 (16 tools, 4 hooks)

fabric_brief()
# → Should show recent fabric entries (initially empty)

qdrant_search("test query")
# → Should return results from knowledge_base (if wiki has content)

fact_store(action='probe', entity='test')
# → Should return empty (no facts stored yet)
```

## What to expect

**Day 1:** Infrastructure running. Fabric entries begin accumulating at session end. Qdrant indexing starts as wiki files are added.

**Week 1:** Context injection active. Agent references past decisions automatically. Wiki pipeline producing curated pages from raw documents.

**Month 1:** Decay scanner has aged content to evaluate. Structured facts accumulating with trust scores.

## Troubleshooting

### Qdrant collection shows 0 points
Check: `EMBEDDING_DIMS=4096` matches collection schema. Mismatch → vectors rejected silently.

### Fabric entries are truncated
Check: `ICARUS_EXTRACTION_MAX_TOKENS=4096` in `.env` AND gateway was restarted after setting it.

### Memory tool reports "Icarus write conflict"
Icarus is writing to MEMORY.md instead of CREATIVE.md. Verify Icarus fork is installed (not upstream esaradev version).

### Context injection not working
Check: OpenRouter API key is set, `context_enhancer.py` can import, gateway restarted after `hooks.py` edits.

### Decay scanner produces "0 archived" every week
Most likely: point payloads missing `last_accessed_at` or `importance_score` metadata. Run backfill before enabling decay.
