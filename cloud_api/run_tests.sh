#!/usr/bin/env bash
# Runs the test suite in a throwaway container (host Python is too old).
# First run downloads wheels into .wheels/; later runs are offline.
set -euo pipefail
cd "$(dirname "$0")"
[ -d .wheels ] || docker run --rm -v "$PWD":/w -w /w python:3.10-bookworm pip download -q -d .wheels -r requirements-dev.txt
docker run --rm -v "$PWD":/w -w /w python:3.10-bookworm bash -c \
  "pip install -q --root-user-action=ignore --no-index -f .wheels -r requirements-dev.txt && python -m pytest $*"
