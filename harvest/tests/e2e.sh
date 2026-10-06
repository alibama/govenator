#!/usr/bin/env bash
# End-to-end without a real model: mock Ollama + daemon, index a page, wait for
# both models to parse it and the site to organize, then print the org chart.
set -euo pipefail
cd "$(dirname "$0")/.."
DB=$(mktemp -d)/e2e.db
python3 tests/mock_ollama.py --port 11556 & MOCK=$!
python3 daemon/harvestd.py --db "$DB" --port 8798 --ollama http://127.0.0.1:11556 \
  --models "qwen3.6,second-model" --quiet-seconds 1 > /dev/null 2>&1 & DAEMON=$!
trap 'kill $MOCK $DAEMON 2>/dev/null' EXIT
for i in $(seq 1 40); do curl -sf http://127.0.0.1:8798/health >/dev/null && break; sleep 0.25; done
curl -sf -X POST -H 'content-type: application/json' --data @tests/fixture_page.json http://127.0.0.1:8798/pages; echo
for i in $(seq 1 60); do
  n=$(python3 -c "import sqlite3;print(sqlite3.connect('$DB').execute('select count(*) from site_model').fetchone()[0])")
  [ "$n" -gt 0 ] && break; sleep 0.5
done
python3 - "$DB" << 'PY'
import sys, json, sqlite3
sys.path.insert(0, "daemon"); import harvestd as H
c = sqlite3.connect(sys.argv[1]); c.row_factory = sqlite3.Row
tree = json.loads(c.execute("select data from site_model order by id desc limit 1").fetchone()["data"])
print(tree["name"], tree["stats"]); print(H.tree_to_outline(tree))
names = {n["name"] for n in tree["children"]}
assert {"Public Works", "Police Department", "Town Clerk"} <= names, names
pw = next(n for n in tree["children"] if n["name"] == "Public Works")
assert {"Streets Division", "Water Division"} <= {n["name"] for n in pw["children"]}
print("E2E OK")
PY
