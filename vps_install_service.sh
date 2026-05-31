#!/usr/bin/env bash
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVICE_NAME="binance-signal-bot"
PYTHON_BIN="$(command -v python3 || true)"

if [[ -z "$PYTHON_BIN" ]]; then
  echo "python3 bulunamadi. Ubuntu/Debian icin: apt update && apt install -y python3"
  exit 1
fi

if [[ ! -f "$APP_DIR/.env" ]]; then
  echo ".env bulunamadi. Telegram token ve chat id olmadan servis baslatilamaz."
  exit 1
fi

if [[ "$(id -u)" -eq 0 ]]; then
  SUDO=""
else
  SUDO="sudo"
fi

SERVICE_USER="$(id -un)"
SERVICE_GROUP="$(id -gn)"

$SUDO tee "/etc/systemd/system/${SERVICE_NAME}.service" >/dev/null <<EOF
[Unit]
Description=Binance Signal Telegram Bot
Wants=network-online.target
After=network-online.target

[Service]
Type=oneshot
User=${SERVICE_USER}
Group=${SERVICE_GROUP}
WorkingDirectory=${APP_DIR}
Environment=PYTHONUNBUFFERED=1
ExecStart=${PYTHON_BIN} ${APP_DIR}/main.py --once

[Install]
WantedBy=multi-user.target
EOF

$SUDO tee "/etc/systemd/system/${SERVICE_NAME}.timer" >/dev/null <<EOF
[Unit]
Description=Run Binance Signal Telegram Bot every 5 minutes

[Timer]
OnBootSec=1min
OnUnitActiveSec=5min
AccuracySec=30s
Persistent=true
Unit=${SERVICE_NAME}.service

[Install]
WantedBy=timers.target
EOF

$SUDO systemctl daemon-reload
$SUDO systemctl enable --now "${SERVICE_NAME}.timer"
$SUDO systemctl start "${SERVICE_NAME}.service" || true

echo "Kuruldu."
echo "Durum: systemctl status ${SERVICE_NAME}.timer --no-pager"
echo "Log: journalctl -u ${SERVICE_NAME}.service -n 80 --no-pager"
