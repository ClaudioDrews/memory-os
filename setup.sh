#!/usr/bin/env bash
# ──────────────────────────────────────────────────────────────────────────────
# Memory OS — Setup Script
# ──────────────────────────────────────────────────────────────────────────────
# Installs the complete Memory OS stack into your Hermes Agent.
#
# Usage:
#   curl -sSL https://raw.githubusercontent.com/ClaudioDrews/memory-os/main/setup.sh | bash
#
# Or, if you already cloned the repo:
#   bash setup.sh
#
# What this script does:
#   1. Checks prerequisites (Docker, Python, Hermes)
#   2. Clones the repo (if needed)
#   3. Installs Python dependencies
#   4. Creates SQLite databases (state.db, memory_store.db)
#   5. Installs the Icarus plugin
#   6. Creates wiki/vault directory structure
#   7. Starts Redis + Qdrant + Worker (Docker Compose)
#   8. Configures environment variables
#   9. Applies rulebook modifications
#
# Idempotent — safe to run multiple times.
# ──────────────────────────────────────────────────────────────────────────────
set -euo pipefail

# ── Colors ────────────────────────────────────────────────────────────────────
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
BOLD='\033[1m'
NC='\033[0m'

PASS=0
FAIL=0
WARN=0

ok()  { printf "  ${GREEN}✅${NC} %s\n" "$1"; PASS=$((PASS + 1)); }
fail() { printf "  ${RED}❌${NC} %s\n" "$1"; FAIL=$((FAIL + 1)); }
warn() { printf "  ${YELLOW}⚠️${NC}  %s\n" "$1"; WARN=$((WARN + 1)); }
info() { printf "  📘 %s\n" "$1"; }

banner() {
    echo ""
    echo -e "${BOLD}── $1 ──${NC}"
    echo ""
}

# ── Detect script directory ──────────────────────────────────────────────────
# When run via curl|bash, SCRIPT_DIR is the current directory.
# When run from a cloned repo, it's the script's location.
if [ -n "${BASH_SOURCE[0]:-}" ] && [ "${BASH_SOURCE[0]}" != "bash" ]; then
    SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
else
    SCRIPT_DIR="$(pwd)"
fi

REPO_URL="https://github.com/ClaudioDrews/memory-os.git"
REPO_DIR="${HOME}/memory-os"
HERMES_HOME="${HOME}/.hermes"
VAULT_PATH="${VAULT_PATH:-${HOME}/vault}"
ENV_FILE="${HERMES_HOME}/.env"

# ──────────────────────────────────────────────────────────────────────────────
# Phase 1: Bootstrap — clone repo if needed
# ──────────────────────────────────────────────────────────────────────────────
banner "Phase 1: Bootstrap"

if [ -d "${REPO_DIR}/.git" ]; then
    ok "Repo already exists at ${REPO_DIR}"
    cd "${REPO_DIR}"
else
    info "Cloning Memory OS..."
    git clone "${REPO_URL}" "${REPO_DIR}" 2>&1 | tail -1
    cd "${REPO_DIR}"
    ok "Repo cloned to ${REPO_DIR}"
fi

# ──────────────────────────────────────────────────────────────────────────────
# Phase 2: Pre-flight Checks
# ──────────────────────────────────────────────────────────────────────────────
banner "Phase 2: Pre-flight Checks"

# Docker
if docker info >/dev/null 2>&1; then
    ok "Docker $(docker --version | awk '{print $3}' | tr -d ',')"
else
    fail "Docker is not running — install and start Docker first"
    exit 1
fi

# Docker Compose
if docker compose version >/dev/null 2>&1; then
    ok "Docker Compose $(docker compose version --short 2>/dev/null || echo 'ok')"
else
    warn "Docker Compose plugin not detected — required to start the stack"
fi

# Python
PYTHON_VERSION=$(python3 --version 2>/dev/null | awk '{print $2}' || echo "none")
if [ "$PYTHON_VERSION" != "none" ]; then
    ok "Python ${PYTHON_VERSION}"
else
    fail "Python 3 not found"
    exit 1
fi

# Hermes
if command -v hermes >/dev/null 2>&1 || [ -f "${HERMES_HOME}/hermes-agent/cli.py" ]; then
    ok "Hermes Agent detected at ${HERMES_HOME}"
else
    warn "Hermes Agent CLI not found — some features will be limited"
fi

# ──────────────────────────────────────────────────────────────────────────────
# Phase 3: Python Dependencies
# ──────────────────────────────────────────────────────────────────────────────
banner "Phase 3: Python Dependencies"

if [ -f "requirements.txt" ]; then
    # Install in current environment (local user or venv)
    if pip install --user -r requirements.txt --quiet 2>&1 | tail -3; then
        ok "Python dependencies installed"
    else
        warn "pip install failed — trying with --break-system-packages"
        pip install --break-system-packages -r requirements.txt --quiet 2>&1 || {
            fail "Could not install Python dependencies"
            exit 1
        }
        ok "Python dependencies installed (--break-system-packages)"
    fi
else
    fail "requirements.txt not found at $(pwd)"
    exit 1
fi

# ──────────────────────────────────────────────────────────────────────────────
# Phase 4: SQLite Databases
# ──────────────────────────────────────────────────────────────────────────────
banner "Phase 4: Database Setup"

if [ -f "setup/setup_db.py" ]; then
    python3 setup/setup_db.py 2>&1 && \
        ok "SQLite databases created (state.db, memory_store.db)" || \
        fail "setup_db.py failed"
else
    fail "setup/setup_db.py not found"
    exit 1
fi

# ──────────────────────────────────────────────────────────────────────────────
# Phase 5: Icarus Plugin
# ──────────────────────────────────────────────────────────────────────────────
banner "Phase 5: Icarus Plugin"

ICARUS_DEST="${HERMES_HOME}/plugins/icarus"

if [ -d "icarus" ]; then
    mkdir -p "${HERMES_HOME}/plugins"
    cp -r icarus/ "${ICARUS_DEST}/"
    ok "Icarus plugin installed at ${ICARUS_DEST}"
else
    fail "icarus/ directory not found"
    exit 1
fi

# ──────────────────────────────────────────────────────────────────────────────
# Phase 6: Wiki + Vault Structure (BEFORE Docker — prevents root ownership)
# ──────────────────────────────────────────────────────────────────────────────
banner "Phase 6: Wiki & Vault"

mkdir -p "${VAULT_PATH}/wiki/"{raw,concepts,entities,comparisons,_meta,_archive}
mkdir -p "${VAULT_PATH}/fabric"
ok "Directory structure created at ${VAULT_PATH}"

# ──────────────────────────────────────────────────────────────────────────────
# Phase 7: Docker Stack
# ──────────────────────────────────────────────────────────────────────────────
banner "Phase 7: Docker Stack"

DOCKER_DIR="${REPO_DIR}/docker"

if [ ! -d "${DOCKER_DIR}" ]; then
    fail "docker/ directory not found at ${REPO_DIR}"
    exit 1
fi

cd "${DOCKER_DIR}"

# Detect OpenRouter API key from Hermes .env
OPENROUTER_KEY=""
if [ -f "${ENV_FILE}" ]; then
    # Try OPENROUTER_DS_API_KEY first (Hermes default), then OPENROUTER_API_KEY
    OPENROUTER_KEY=$(grep -oP 'OPENROUTER_DS_API_KEY=\K.*' "${ENV_FILE}" 2>/dev/null | head -1 || true)
    if [ -z "${OPENROUTER_KEY}" ]; then
        OPENROUTER_KEY=$(grep -oP 'OPENROUTER_API_KEY=\K.*' "${ENV_FILE}" 2>/dev/null | head -1 || true)
    fi
fi

if [ -z "${OPENROUTER_KEY}" ]; then
    echo ""
    echo -e "  ${YELLOW}Could not find your OpenRouter key in Hermes .env.${NC}"
    echo "  The worker needs it to generate embeddings."
    echo ""
    read -r -p "  Paste your OpenRouter key (e.g. sk-or-v1-...): " OPENROUTER_KEY
    echo ""
fi

# Generate random Redis password
REDIS_PW=$(openssl rand -hex 16)

# Create Docker Compose .env
cat > .env << DOCKERENV
OPENROUTER_API_KEY=${OPENROUTER_KEY}
REDIS_PASSWORD=${REDIS_PW}
QDRANT_API_KEY=
EMBEDDING_DIMS=4096
COLLECTION_NAME=knowledge_base
LOG_LEVEL=INFO
MEMORY_OS_WIKI_PATH=${VAULT_PATH}/wiki
MEMORY_OS_HERMES_HOME=${HERMES_HOME}
MEMORY_OS_FABRIC_DIR=${VAULT_PATH}/fabric
DOCKERENV

ok "docker/.env created"

# Pull pre-built images first (Redis, Qdrant) — fast
info "Downloading pre-built images (Redis, Qdrant)..."
docker compose pull redis qdrant 2>&1 | tail -3
ok "Base images downloaded"

# Build worker image — SLOW on first run (gcc + build-essential)
info "Building worker image (may take 5-10 minutes on first run)..."
info "  (Future builds will use Docker cache)"
if docker compose build worker 2>&1; then
    ok "Worker image built"
else
    fail "Failed to build worker image"
    exit 1
fi

# Start everything
info "Starting containers..."
if docker compose up -d 2>&1; then
    ok "Docker stack started (redis, qdrant, worker)"
else
    fail "docker compose up failed — check Docker"
    exit 1
fi

# Wait for healthy
info "Waiting for services to become healthy..."
sleep 3
if docker compose ps --format json 2>/dev/null | grep -q '"Health":"healthy"'; then
    ok "All services healthy"
else
    warn "Services may still be starting — check with: docker compose ps"
fi

# Return to repo directory
cd "${REPO_DIR}"

# ──────────────────────────────────────────────────────────────────────────────
# Phase 8: Environment Variables
# ──────────────────────────────────────────────────────────────────────────────
banner "Phase 8: Hermes .env"

if [ ! -f "${ENV_FILE}" ]; then
    warn "${ENV_FILE} not found — creating a new one"
    touch "${ENV_FILE}"
fi

add_env() {
    local key="$1"
    local value="$2"
    if grep -q "^${key}=" "${ENV_FILE}" 2>/dev/null; then
        # Already exists — don't overwrite
        return 0
    fi
    echo "${key}=${value}" >> "${ENV_FILE}"
}

add_env "FABRIC_DIR" "${VAULT_PATH}/fabric"
add_env "ICARUS_EXTRACTION_MAX_TOKENS" "4096"
add_env "ICARUS_EXTRACTION_MODEL" "deepseek/deepseek-v4-flash"
add_env "EMBEDDING_DIMS" "4096"
add_env "HERMES_AGENT_NAME" "hermes"
add_env "REDIS_PASSWORD" "${REDIS_PW}"
add_env "OPENROUTER_DS_API_KEY" "${OPENROUTER_KEY}"

ok "Environment variables added to Hermes .env"

# ──────────────────────────────────────────────────────────────────────────────
# Phase 9: Rulebook Modifications
# ──────────────────────────────────────────────────────────────────────────────
banner "Phase 9: Rulebook"

RULEBOOK="${HERMES_HOME}/rulebook.md"

if [ -f "${RULEBOOK}" ]; then
    if grep -q "Memory OS amendment" "${RULEBOOK}" 2>/dev/null; then
        ok "Rulebook amendments already applied"
    else
        info "Applying Memory OS amendments to rulebook..."
        info "(See modifications/execution-agent-protocol.md for details)"
        warn "Amendments NOT applied automatically — edit the rulebook manually"
        warn "See: ${REPO_DIR}/modifications/execution-agent-protocol.md"
    fi
else
    warn "${RULEBOOK} not found — skipping modifications"
fi

# ──────────────────────────────────────────────────────────────────────────────
# Phase 10: Gateway
# ──────────────────────────────────────────────────────────────────────────────
banner "Phase 10: Gateway"

if command -v hermes >/dev/null 2>&1; then
    info "Restarting Hermes gateway..."
    if hermes gateway restart 2>&1; then
        ok "Gateway restarted"
    else
        warn "Gateway restart failed — restart manually with: hermes gateway restart"
    fi
else
    warn "'hermes' command not available — restart the gateway manually"
fi

# ──────────────────────────────────────────────────────────────────────────────
# Summary
# ──────────────────────────────────────────────────────────────────────────────
banner "Summary"

echo "  Passed:  ${PASS}"
echo "  Failed:  ${FAIL}"
echo "  Warnings: ${WARN}"
echo ""

if [ "${FAIL}" -eq 0 ]; then
    echo -e "  ${GREEN}${BOLD}✅ Memory OS installed successfully!${NC}"
    echo ""
    echo "  To verify:"
    echo "    • /plugins          → should show 'icarus'"
    echo "    • docker compose ps → 3 services (redis, qdrant, worker)"
    echo "    • fabric_brief()    → fabric entries (initially empty)"
    echo "    • qdrant_search()   → semantic search (requires populated wiki)"
    echo ""
    echo "  Next step: add .md files to ${VAULT_PATH}/wiki/raw/"
    echo "  and the ingestion pipeline will index them automatically."
    echo ""
else
    echo -e "  ${RED}${BOLD}❌ ${FAIL} error(s) found — review the output above.${NC}"
    exit 1
fi
