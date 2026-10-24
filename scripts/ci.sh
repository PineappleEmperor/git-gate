#!/bin/sh
# Local CI: lint, format check, tests. Run from the repo root with the dev .venv in place.
set -eu
cd "$(dirname "$0")/.."
.venv/bin/ruff check .
.venv/bin/ruff format --check .
.venv/bin/pytest -q
