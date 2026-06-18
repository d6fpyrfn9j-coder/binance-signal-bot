#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

export PYTHONPYCACHEPREFIX="${TMPDIR:-/private/tmp}/codex_pycache"

exec /Users/eminaliyev/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3 main.py --once
