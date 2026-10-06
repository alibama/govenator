#!/usr/bin/env bash
# Everything on one machine: Harvest (Ollama), the bridge, and the Scout app, sharing
# local files. For a shared Turso database, set SCOUT_DB/SCOUT_DB_TOKEN first.
set -euo pipefail
cd "$(dirname "$0")/.."
python3 harvest/daemon/harvestd.py --models "${HARVEST_MODELS:-qwen3.6}" & HARVEST=$!
python3 bridge/bridge.py & BRIDGE=$!
trap 'kill $HARVEST $BRIDGE 2>/dev/null' EXIT
streamlit run scout/app.py
