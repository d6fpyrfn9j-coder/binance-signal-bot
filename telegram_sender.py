"""Telegram Bot API sender."""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request


def _clean_token(value: str) -> str:
    allowed = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789:_-")
    return "".join(char for char in value if char in allowed)


def _clean_chat_id(value: str) -> str:
    allowed = set("0123456789-")
    return "".join(char for char in value if char in allowed)


def send_telegram_message(token: str, chat_id: str, text: str) -> None:
    token = _clean_token(token)
    chat_id = _clean_chat_id(chat_id)
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = json.dumps(
        {
            "chat_id": chat_id,
            "text": text,
            "disable_web_page_preview": True,
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            response.read()
        logging.info("Telegram report sent")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Telegram HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Telegram network error: {exc.reason}") from exc
