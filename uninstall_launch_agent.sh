#!/usr/bin/env bash
set -euo pipefail

LABEL="com.eminaliyev.binance-signal-bot"
TARGET="/Users/eminaliyev/Library/LaunchAgents/$LABEL.plist"
USER_ID="$(id -u)"

launchctl bootout "gui/$USER_ID" "$TARGET" 2>/dev/null || true
rm -f "$TARGET"

echo "LaunchAgent kaldirildi: $LABEL"

