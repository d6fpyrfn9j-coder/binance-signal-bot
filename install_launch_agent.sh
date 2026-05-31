#!/usr/bin/env bash
set -euo pipefail

LABEL="com.eminaliyev.binance-signal-bot"
PLIST_NAME="$LABEL.plist"
WORKDIR="/Users/eminaliyev/Documents/bot binance"
TARGET_DIR="/Users/eminaliyev/Library/LaunchAgents"
TARGET="$TARGET_DIR/$PLIST_NAME"
USER_ID="$(id -u)"

mkdir -p "$TARGET_DIR"
cp "$WORKDIR/$PLIST_NAME" "$TARGET"

launchctl bootout "gui/$USER_ID" "$TARGET" 2>/dev/null || true
launchctl bootstrap "gui/$USER_ID" "$TARGET"
launchctl enable "gui/$USER_ID/$LABEL"
launchctl kickstart -k "gui/$USER_ID/$LABEL"

echo "LaunchAgent kuruldu: $LABEL"
echo "Her 5 dakikada bir Telegram raporu gonderecek."

