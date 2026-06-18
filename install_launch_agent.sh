#!/usr/bin/env bash
set -euo pipefail

LABEL="com.eminaliyev.binance-signal-bot"
PLIST_NAME="$LABEL.plist"
WORKDIR="/Users/eminaliyev/Documents/bot binance"
RUNTIME_DIR="/Users/eminaliyev/Library/Application Support/binance-signal-bot"
LOG_DIR="/Users/eminaliyev/Library/Logs/binance-signal-bot"
TARGET_DIR="/Users/eminaliyev/Library/LaunchAgents"
TARGET="$TARGET_DIR/$PLIST_NAME"
USER_ID="$(id -u)"

mkdir -p "$RUNTIME_DIR" "$LOG_DIR"
rsync -a \
  --exclude ".git/" \
  --exclude "__pycache__/" \
  --exclude ".venv/" \
  --exclude "bot.log" \
  --exclude "launchd.out.log" \
  --exclude "launchd.err.log" \
  "$WORKDIR/" "$RUNTIME_DIR/"
chmod +x "$RUNTIME_DIR/launch_bot.sh"

mkdir -p "$TARGET_DIR"
cp "$WORKDIR/$PLIST_NAME" "$TARGET"

launchctl bootout "gui/$USER_ID" "$TARGET" 2>/dev/null || true
launchctl bootstrap "gui/$USER_ID" "$TARGET"
launchctl enable "gui/$USER_ID/$LABEL"
launchctl kickstart -k "gui/$USER_ID/$LABEL"

echo "LaunchAgent kuruldu: $LABEL"
echo "Her 5 dakikada bir Telegram raporu gonderecek."
