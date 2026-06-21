"""Consecutive-confirmation lock for futures signals.

The bot is stateless between 5-minute reports, so a raw signal can flip
LONG -> BEKLE -> SHORT on small noise (the "AÇ SHORT" then "BEKLE" problem).

This module persists, per symbol, how many consecutive reports a directional
side has held. A side only counts as *confirmed* (tradable "AÇ") after
REQUIRED_CONFIRMATIONS consecutive agreeing reports. Until then the signal is
shown as BEKLE, so the bot stops changing its mind on every small wave.

State lives in a small JSON file next to the history file. On Render the file
is reset on redeploy, which is fine: after a restart a side simply needs its
confirmations again before going live.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
from pathlib import Path
from typing import Any


DEFAULT_LOCK_FILE = "futures_signal_lock.json"
REQUIRED_CONFIRMATIONS = max(1, int(os.getenv("FUTURES_SIGNAL_CONFIRMATIONS", "2")))
# If the gap since the last update is larger than this, the streak is stale
# (bot was down / Render restarted) and counting restarts from 1.
MAX_GAP_SECONDS = int(os.getenv("FUTURES_SIGNAL_LOCK_MAX_GAP", "1200"))


def _lock_path() -> Path:
    return Path(os.getenv("FUTURES_SIGNAL_LOCK_FILE", DEFAULT_LOCK_FILE))


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _parse_time(value: Any) -> dt.datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return dt.datetime.fromisoformat(value)
    except ValueError:
        return None


def _load(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        logging.exception("Could not read futures signal lock state")
        return {}
    return data if isinstance(data, dict) else {}


def _save(path: Path, state: dict[str, Any]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        logging.exception("Could not save futures signal lock state")


def update_and_confirm(symbol: str, proposed_side: str) -> tuple[int, bool]:
    """Record this report's proposed side and report confirmation status.

    Returns (streak, confirmed):
      - streak: how many consecutive reports this directional side has held.
      - confirmed: True only when streak >= REQUIRED_CONFIRMATIONS.

    A non-directional proposal ("BEKLE" / anything not LONG/SHORT) breaks the
    chain and returns (0, False).
    """
    path = _lock_path()
    state = _load(path)
    now = _now()
    entry = state.get(symbol)
    if not isinstance(entry, dict):
        entry = {}

    if proposed_side not in {"LONG", "SHORT"}:
        state[symbol] = {"side": "BEKLE", "streak": 0, "updated_at": now.isoformat()}
        _save(path, state)
        return 0, False

    prev_side = entry.get("side")
    prev_streak = int(entry.get("streak") or 0)
    prev_time = _parse_time(entry.get("updated_at"))
    stale = prev_time is None or (now - prev_time).total_seconds() > MAX_GAP_SECONDS

    if stale or prev_side != proposed_side:
        streak = 1
    else:
        streak = prev_streak + 1

    state[symbol] = {"side": proposed_side, "streak": streak, "updated_at": now.isoformat()}
    _save(path, state)
    return streak, streak >= REQUIRED_CONFIRMATIONS
