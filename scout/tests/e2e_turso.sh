#!/usr/bin/env bash
# The same end-to-end test, with the database behind a Turso-compatible HTTP server.
set -euo pipefail
cd "$(dirname "$0")/.."
TMP=$(mktemp -d)
python3 tests/mock_turso.py --port 9092 --db "$TMP/turso.db" --token e2e & TURSO=$!
trap 'kill $TURSO 2>/dev/null' EXIT
sleep 1
SCOUT_DB_TOKEN=e2e E2E_DB=http://127.0.0.1:9092 bash tests/e2e.sh
