#!/usr/bin/env bash
# Run the same checks as .github/workflows/ci.yml on this machine.
set -euo pipefail
cd "$(dirname "$0")/.."

python scripts/fetch_test_data.py
ruff check .
ruff format --check .
mypy slidetwin
if command -v node >/dev/null; then
  node --test tests/web/*.test.mjs
else
  echo "node not found: skipping the UI unit tests" >&2
fi
SLIDETWIN_REQUIRE_TESTDATA=1 pytest -q --cov --cov-report=term-missing --cov-fail-under=100
