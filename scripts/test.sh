#!/bin/sh
set -eu
PROJECT_ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$PROJECT_ROOT"
python3 -m compileall -q collector proxy tests
python3 -m unittest discover -v
node --test packages/acp-observer/test/*.test.mjs
npm run check
python3 scripts/check-secrets.py
