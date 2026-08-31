#!/bin/sh
set -eu
PROJECT_ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$PROJECT_ROOT"
python3 -m compileall -q collector tests
python3 -m unittest discover -s tests -v
python3 scripts/check-secrets.py
