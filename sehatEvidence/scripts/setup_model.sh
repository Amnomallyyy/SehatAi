#!/usr/bin/env bash
# setup_model.sh -- EvidenceBoard LLM setup checklist (Unix / Git Bash users).
#
# This is a documented helper, not core code: it prints the NVIDIA NIM setup
# steps (the primary path) and the optional local Ollama fallback. The Windows
# dev machine configures everything through .env instead.
#
# Usage:
#   bash scripts/setup_model.sh            # print the setup checklist
#   bash scripts/setup_model.sh --check    # probe LLM_BASE_URL connectivity

set -euo pipefail

# Resolve the project root relative to this script so .env is found
# regardless of the current working directory.
script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
project_root="$(dirname "$script_dir")"

echo "============================================================"
echo " EvidenceBoard -- LLM setup checklist"
echo "============================================================"
echo
echo "[1] NVIDIA NIM (primary path)"
echo "    a. Create a free account at https://build.nvidia.com/"
echo "    b. Generate an API key (it looks like nvapi-...)."
echo "       Multiple free accounts -> multiple keys -> automatic failover."
echo "    c. Copy .env.example to .env and put the key(s) in it:"
echo "         cp .env.example .env"
echo "       Single key:"
echo "         LLM_API_KEY=nvapi-REPLACE_WITH_YOUR_KEY"
echo "       Multiple keys (comma-separated, takes precedence):"
echo "         LLM_API_KEYS=nvapi-REPLACE_WITH_YOUR_KEY_1,nvapi-REPLACE_WITH_YOUR_KEY_2"
echo "    d. Defaults already match NIM (edit only if you want changes):"
echo "         LLM_BASE_URL=https://integrate.api.nvidia.com/v1"
echo "         LLM_MODEL=meta/llama-3.3-70b-instruct"
echo
echo "[2] Optional local Ollama fallback (when NIM is unreachable)"
echo "    a. Install Ollama: https://ollama.com/download"
echo "    b. Pull a model:"
echo "         ollama pull qwen2.5:7b-instruct"
echo "    c. Point .env at the local server:"
echo "         LLM_BASE_URL=http://localhost:11434/v1"
echo "         LLM_MODEL=qwen2.5:7b-instruct"
echo "       (Leave LLM_API_KEY unset; local Ollama ignores auth.)"
echo

if [[ "${1:-}" == "--check" ]]; then
    # Connectivity probe: read LLM_BASE_URL from the environment, falling
    # back to the .env file (curl -s $LLM_BASE_URL/models | head -c 300 style).
    base_url="${LLM_BASE_URL:-}"
    if [[ -z "$base_url" && -f "$project_root/.env" ]]; then
        base_url="$(grep -E '^LLM_BASE_URL=' "$project_root/.env" \
            | tail -n 1 | cut -d= -f2- | tr -d '\r" ' || true)"
    fi
    if [[ -z "$base_url" ]]; then
        echo "[check] LLM_BASE_URL is not set (env or .env); nothing to probe."
        echo "[check] Tip: LLM_BASE_URL=https://integrate.api.nvidia.com/v1 bash $0 --check"
        exit 0
    fi
    echo "[check] Probing ${base_url}/models (15s timeout) ..."
    probe="$(curl -s --max-time 15 "${base_url}/models" || true)"
    echo "${probe:0:300}"
    echo
    if [[ -z "$probe" ]]; then
        echo "[check] No response -- endpoint unreachable. Check the network/URL."
    else
        echo "[check] Got a response -- the endpoint is reachable."
        echo "[check] (An auth-error body still proves connectivity; add a key.)"
    fi
else
    echo "Tip: verify connectivity with:"
    echo "    LLM_BASE_URL=https://integrate.api.nvidia.com/v1 bash scripts/setup_model.sh --check"
fi
