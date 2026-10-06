#!/usr/bin/env bash
# The Mac side of a shared deployment: Harvest + Ollama, and the bridge to Turso.
# Needs SCOUT_DB (libsql://…) and SCOUT_DB_TOKEN in the environment or a .env file.
set -euo pipefail
cd "$(dirname "$0")/.."
[ -f .env ] && { set -a; . ./.env; set +a; }
: "${SCOUT_DB:?set SCOUT_DB to your Turso URL}"
python3 harvest/daemon/harvestd.py --models "${HARVEST_MODELS:-qwen3.6}" & HARVEST=$!
trap 'kill $HARVEST 2>/dev/null' EXIT
python3 bridge/bridge.py
