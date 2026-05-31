#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

if [[ ! -f bot.pid ]]; then
  echo "bot.pid yok. Bot zaten kapali olabilir."
  exit 0
fi

PID="$(cat bot.pid)"
if kill -0 "$PID" 2>/dev/null; then
  kill "$PID"
  echo "Bot durduruldu. PID: $PID"
else
  echo "PID calismiyor: $PID"
fi

rm -f bot.pid

