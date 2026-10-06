#!/usr/bin/env bash
# Run every test in the repo.
set -euo pipefail
cd "$(dirname "$0")/.."
bash scout/tests/run_all.sh
python3 harvest/tests/test_harvestd.py
bash harvest/tests/e2e.sh
echo "ALL TESTS PASSED"
