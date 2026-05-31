#!/usr/bin/env python3
"""Print recent Telegram chat ids for the configured bot."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from pathlib import Path


def load_env(path: str = ".env") -> None:
    env_path = Path(path)
    if not env_path.exists():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def main() -> int:
    load_env()
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        print("TELEGRAM_BOT_TOKEN .env icinde yok")
        return 1

    url = f"https://api.telegram.org/bot{token}/getUpdates"
    request = urllib.request.Request(url)
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            data = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        print(f"Telegram HTTP {exc.code}: {detail}")
        return 1
    except urllib.error.URLError as exc:
        print(f"Telegram network error: {exc.reason}")
        return 1

    updates = data.get("result", [])
    if not updates:
        print("Henuz mesaj yok. Telegram'da botuna once 'test' yaz.")
        return 1

    seen: set[int] = set()
    for update in updates:
        message = update.get("message") or update.get("channel_post") or {}
        chat = message.get("chat", {})
        chat_id = chat.get("id")
        if chat_id is None or chat_id in seen:
            continue
        seen.add(chat_id)
        title = chat.get("title") or chat.get("username") or chat.get("first_name") or "isimsiz"
        print(f"CHAT_ID={chat_id} | {title}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
