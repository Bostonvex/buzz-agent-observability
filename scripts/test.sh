#!/bin/sh
set -eu
PROJECT_ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$PROJECT_ROOT"
python3 -m compileall -q collector proxy tests
python3 -m py_compile scripts/artifact-version.py scripts/check-release.py
python3 -m unittest discover -v
node --test packages/acp-observer/test/*.test.mjs
npm run check
python3 scripts/check-secrets.py
sh -n scripts/install.sh scripts/upgrade.sh scripts/rollback.sh scripts/uninstall.sh scripts/build-release.sh scripts/test-install.sh
