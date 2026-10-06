#!/usr/bin/env bash
# Every Scout test: unit, flows (manual input, exports, bridge), the fake-world crawl in
# local and Turso mode, and a headless run of the app.
set -euo pipefail
cd "$(dirname "$0")/.."
python3 tests/test_scout.py
python3 tests/test_flows.py
bash tests/e2e.sh
bash tests/e2e_turso.sh
TMP=$(mktemp -d)
E2E_DB="$TMP/app.db" bash tests/e2e.sh > /dev/null
python3 - "$TMP/app.db" << 'PY'
import json, sys
sys.path.insert(0, "."); sys.path.insert(0, "tests")
import scout
from test_flows import TREE
c = scout.open_db(sys.argv[1])
c.execute("UPDATE jurisdiction SET qid='Q900001' WHERE jid='P5199901'")
c.execute("INSERT OR REPLACE INTO orgchart VALUES('exampleville.gov','P5199901','qwen3.6',?,'h','now','now')", (json.dumps(TREE),))
c.commit()
PY
SCOUT_DB="$TMP/app.db" python3 tests/app_smoke.py local 2>&1 | grep -v ScriptRunContext
