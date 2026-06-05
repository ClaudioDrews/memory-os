# Memory OS — Quick Start

## One-command install

```bash
curl -sSL https://raw.githubusercontent.com/ClaudioDrews/memory-os/main/setup.sh | bash
```

This installs everything: Docker stack (Redis + Qdrant + Worker), Icarus plugin, SQLite databases, wiki vault, and environment variables. Safe to re-run — all steps are idempotent.

**Requires:** Docker, Python 3.11+, Hermes Agent. The script auto-detects your OpenRouter key and prompts only if missing.

> Prefer manual control? Follow [setup/install.md](setup/install.md) — step-by-step guide with validation checkpoints.

## Prerequisites

- **Docker** (Docker Compose v2)
- **Python 3.11+**
- **Hermes Agent** (v0.14.0 or later)
- **OpenRouter API key** (or local Ollama for embeddings)

## 1. Clone

```bash
git clone https://github.com/ClaudioDrews/memory-os.git
cd memory-os
```

## 2. Install

Follow [setup/install.md](setup/install.md) — step-by-step guide with validation checkpoints.

## 3. Verify

Once installed, confirm the stack is operational:

```bash
# Docker services
docker compose ps    # qdrant + redis + worker should be "healthy"

# Qdrant
curl -s http://localhost:6333/healthz    # should return "ok"

# Icarus plugin
hermes plugins list | grep icarus
```

## 4. Use

Open Hermes. From the next session onward, it will:
- Recall past decisions (Icarus Fabric)
- Search your vault documents (Qdrant)
- Cross-reference facts you've mentioned (fact_store)

## 5. Add content

```bash
# Adjust to your vault path
mkdir -p ~/vault/wiki/raw
echo "# My notes" > ~/vault/wiki/raw/notes.md
```

The worker detects and indexes new files automatically.

## Next steps

- Full install guide: [setup/install.md](setup/install.md)
- Architecture: [layers/](layers/)
- How to contribute: [.github/CONTRIBUTING.md](.github/CONTRIBUTING.md)
