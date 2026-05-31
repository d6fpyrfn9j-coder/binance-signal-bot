#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

PYTHON="/Users/eminaliyev/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3"

if [[ -f bot.pid ]] && kill -0 "$(cat bot.pid)" 2>/dev/null; then
  echo "Bot zaten calisiyor. PID: $(cat bot.pid)"
  exit 0
fi

nohup "$PYTHON" main.py >> bot_runner.log 2>&1 &
echo "$!" > bot.pid
echo "Bot basladi. PID: $(cat bot.pid)"

