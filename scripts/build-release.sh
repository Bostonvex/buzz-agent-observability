#!/bin/sh
set -eu
project_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$project_root"
./scripts/test.sh
uv build
python3 scripts/check-release.py --write-checksums
./scripts/test-install.sh dist/buzz_agent_observability-0.1.0-py3-none-any.whl
