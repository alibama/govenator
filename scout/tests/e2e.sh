#!/usr/bin/env bash
# End-to-end on a fake world (no internet needed): Census + .gov fixtures, five
# mock municipal sites, a real crawl with Playwright, then assertions.
set -euo pipefail
cd "$(dirname "$0")/.."
TMP=$(mktemp -d); DB="${E2E_DB:-$TMP/e2e.db}"; LOG="$TMP/web.log"
python3 tests/mock_web.py --port 9078 --log "$LOG" & WEB=$!
trap 'kill $WEB 2>/dev/null' EXIT
M=http://127.0.0.1:9078
export SCOUT_TEST_HOSTS="{\"exampleville.gov\":\"$M\",\"www.exampleville.gov\":\"$M\",\"examplevillepd.gov\":\"$M\",\"testtownva.gov\":\"$M\",\"blockedburg.gov\":\"$M\",\"jsville.gov\":\"$M\",\"examplecounty.gov\":\"$M\"}"
sleep 1
python3 scout.py --db "$DB" setup --gazetteer-dir tests/fixtures --dotgov-csv tests/fixtures/dotgov.csv \
  --geojson data/counties.geojson --no-wikidata
python3 scout.py --db "$DB" run --workers 4 --delay 0.2 --playwright
python3 - "$DB" "$LOG" << 'PY'
import sys, json
sys.path.insert(0, ".")
import scout
c = scout.open_db(sys.argv[1])
got = {r["display"]: (r["stage"], r["label"], r["best_url"]) for r in c.execute(
    "SELECT j.display, j.stage, q.label, q.best_url FROM jurisdiction j LEFT JOIN quality q USING(jid)")}
for k, v in sorted(got.items()): print(f"  {k:28} {v}")
assert got["City of Exampleville, VA"][:2] == ("indexed", "good directory")
assert got["City of Exampleville, VA"][2].endswith("/directory")
assert got["Town of Jsville, VA"][0] == "indexed" and got["Town of Jsville, VA"][2].endswith("/staff")   # rendered by Playwright
assert got["Example County, VA"][0] == "indexed"
assert got["Town of Testtown, VA"][0] == "no_site"          # guessed domain belonged to Testtown, Ohio
assert got["Town of Blockedburg, VA"][0] == "blocked"        # bot challenge: recorded, not evaded
assert got["Capitol Planning Region, CT"][0] == "skip"       # no county government
assert "Somewhere CDP, VA" not in got                         # statistical place, not a government
fetched = [tuple(json.loads(l)) for l in open(sys.argv[2])]
assert ("exampleville.gov", "/private/directory") not in fetched, "robots.txt was not honoured"
assert not any(p in ("/news", "/pay") for _, p in fetched), "crawled pages it should have skipped"
print("E2E OK")
PY
