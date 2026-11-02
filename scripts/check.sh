#!/usr/bin/env bash
# Run locally exactly what CI runs (.github/workflows/ci.yml), plus a shell syntax check.
# Usage: scripts/check.sh   (from the repo root, inside the venv)
# Owner: Muhammad Ahmed Qazi (CS-24045). Issue #29.
set -euo pipefail
cd "$(dirname "$0")/.."

ruff check .
ruff format --check .
pytest -q
for f in scripts/*.sh; do bash -n "$f"; done
echo "check.sh: all checks passed"
