#!/usr/bin/env bash
# Start the daemon (inbox + background parser) and the dashboard together.
#   ./start.sh                         # qwen3.6 only
#   HARVEST_MODELS="qwen3.6,<second>" ./start.sh
set -euo pipefail
cd "$(dirname "$0")"
python3 daemon/harvestd.py --models "${HARVEST_MODELS:-qwen3.6}" &
DAEMON=$!
trap 'kill $DAEMON 2>/dev/null' EXIT
streamlit run dashboard/app.py
