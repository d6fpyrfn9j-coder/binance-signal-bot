#!/usr/bin/env bash
set -euo pipefail

if [[ $# -lt 1 ]]; then
  echo "Kullanim: ./deploy_to_vps.sh root@VPS_IP"
  exit 1
fi

REMOTE="$1"
REMOTE_DIR="/opt/binance-signal-bot"

FILES=(
  ".env"
  ".env.example"
  "README.md"
  "main.py"
  "data_fetcher.py"
  "indicators.py"
  "analyzer.py"
  "telegram_sender.py"
  "telegram_setup.py"
  "vps_install_service.sh"
)

ssh "$REMOTE" "mkdir -p '$REMOTE_DIR'"
scp "${FILES[@]}" "$REMOTE:$REMOTE_DIR/"
ssh "$REMOTE" "chmod +x '$REMOTE_DIR/vps_install_service.sh' && cd '$REMOTE_DIR' && ./vps_install_service.sh"

echo "VPS aktarimi tamamlandi: ${REMOTE}:${REMOTE_DIR}"
