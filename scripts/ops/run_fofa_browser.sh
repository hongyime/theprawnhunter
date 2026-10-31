#!/usr/bin/env bash
# Run the FOFA browser lane from any working directory.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/../.."
export PYTHONUNBUFFERED=1

PYTHON="${PYTHON:-.venv/bin/python}"
if [ ! -x "$PYTHON" ]; then
    PYTHON=python3
fi

exec "$PYTHON" scripts/ops/fofa_browser.py "$@"
