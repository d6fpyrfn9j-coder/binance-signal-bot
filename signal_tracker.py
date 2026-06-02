"""Persist and score manual trade signals over time."""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any


MAX_HISTORY = 250
DEFAULT_HISTORY_FILE = "signal_history.json"


@dataclass(frozen=True)
class SignalCandidate:
    symbol: str
    entry: float
    target: float
    stop: float
    current_price: float
    recent_high: float
    recent_low: float
    confidence: int
    rr: float
    active: bool


@dataclass(frozen=True)
class SignalTrackerResult:
    summary_line: str
    symbol_lines: dict[str, str]


def _now_utc() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _parse_time(value: str) -> dt.datetime | None:
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def _fmt_pct(value: float | None) -> str:
    if value is None:
        return "?"
    sign = "+" if value > 0 else ""
    return f"{sign}{value:.1f}%"


def _history_path() -> Path:
    configured = os.getenv("SIGNAL_HISTORY_FILE", DEFAULT_HISTORY_FILE)
    return Path(configured)


def _load_records(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        logging.exception("Could not read signal history")
        return []
    if isinstance(payload, list):
        return [record for record in payload if isinstance(record, dict)]
    return []


def _save_records(path: Path, records: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    clean_records = records[-MAX_HISTORY:]
    path.write_text(
        json.dumps(clean_records, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _record_pct(record: dict[str, Any], price: float) -> float | None:
    entry = float(record.get("entry") or 0)
    if entry <= 0:
        return None
    return ((price - entry) / entry) * 100


def _update_record(record: dict[str, Any], candidate: SignalCandidate, now: dt.datetime) -> None:
    created_at = _parse_time(str(record.get("created_at", "")))
    if not created_at:
        return

    elapsed = (now - created_at).total_seconds()
    status = str(record.get("status") or "open")
    if status == "open":
        if candidate.recent_low <= float(record.get("stop") or 0):
            record["status"] = "failed"
            record["result"] = "Stop"
            record["closed_at"] = now.isoformat()
        elif candidate.recent_high >= float(record.get("target") or 0):
            record["status"] = "success"
            record["result"] = "Hedef"
            record["closed_at"] = now.isoformat()

    if record.get("pct_1h") is None and elapsed >= 3600:
        record["pct_1h"] = _record_pct(record, candidate.current_price)
    if record.get("pct_4h") is None and elapsed >= 14_400:
        pct_4h = _record_pct(record, candidate.current_price)
        record["pct_4h"] = pct_4h
        if record.get("status") == "open" and pct_4h is not None:
            record["status"] = "success" if pct_4h > 0 else "failed"
            record["result"] = "4s test"
            record["closed_at"] = now.isoformat()


def _is_duplicate(record: dict[str, Any], candidate: SignalCandidate, now: dt.datetime) -> bool:
    if record.get("symbol") != candidate.symbol or record.get("side") != "AL":
        return False

    created_at = _parse_time(str(record.get("created_at", "")))
    if not created_at:
        return False

    elapsed = (now - created_at).total_seconds()
    if record.get("status") == "open":
        return True
    if elapsed > 3600:
        return False

    old_entry = float(record.get("entry") or 0)
    if old_entry <= 0:
        return False
    return abs(candidate.entry - old_entry) / old_entry <= 0.005


def _new_record(candidate: SignalCandidate, now: dt.datetime) -> dict[str, Any]:
    return {
        "symbol": candidate.symbol,
        "side": "AL",
        "created_at": now.isoformat(),
        "entry": candidate.entry,
        "target": candidate.target,
        "stop": candidate.stop,
        "confidence": candidate.confidence,
        "rr": candidate.rr,
        "status": "open",
        "pct_1h": None,
        "pct_4h": None,
        "result": None,
    }


def _latest_symbol_line(records: list[dict[str, Any]], symbol: str) -> str | None:
    latest = next((record for record in reversed(records) if record.get("symbol") == symbol), None)
    if not latest:
        return None

    status = str(latest.get("status") or "open")
    pct_1h = latest.get("pct_1h")
    pct_4h = latest.get("pct_4h")
    created_at = _parse_time(str(latest.get("created_at", "")))
    fresh = bool(created_at and (_now_utc() - created_at).total_seconds() < 600)
    if status == "success":
        suffix = "Başarılı ✅"
    elif status == "failed":
        suffix = "Başarısız 🔴"
    elif fresh:
        suffix = "AL kaydı 🟢"
    else:
        suffix = "Açık 🟡"
    return f"Test: 1s {_fmt_pct(pct_1h)} | 4s {_fmt_pct(pct_4h)} | {suffix}"


def _summary_line(records: list[dict[str, Any]]) -> str:
    closed = [
        record
        for record in records[-100:]
        if record.get("side") == "AL" and record.get("status") in {"success", "failed"}
    ]
    if not closed:
        return "Son 100 sinyal: veri birikiyor 🟡"

    wins = sum(1 for record in closed if record.get("status") == "success")
    rate = (wins / len(closed)) * 100
    emoji = "🟢" if rate >= 60 else "🟡" if rate >= 45 else "🔴"
    return f"Son 100 sinyal: %{rate:.0f} {emoji} ({wins}/{len(closed)})"


def track_signals(candidates: list[SignalCandidate]) -> SignalTrackerResult:
    if os.getenv("SIGNAL_TRACKING_ENABLED", "true").lower() in {"0", "false", "no"}:
        return SignalTrackerResult(summary_line="Son 100 sinyal: kapalı", symbol_lines={})

    path = _history_path()
    records = _load_records(path)
    now = _now_utc()
    candidate_map = {candidate.symbol: candidate for candidate in candidates}

    for record in records:
        symbol = str(record.get("symbol") or "")
        candidate = candidate_map.get(symbol)
        if candidate:
            _update_record(record, candidate, now)

    for candidate in candidates:
        if not candidate.active or candidate.rr < 1:
            continue
        if any(_is_duplicate(record, candidate, now) for record in records):
            continue
        records.append(_new_record(candidate, now))

    try:
        _save_records(path, records)
    except Exception:
        logging.exception("Could not save signal history")

    symbol_lines = {
        candidate.symbol: line
        for candidate in candidates
        if (line := _latest_symbol_line(records, candidate.symbol))
    }
    return SignalTrackerResult(summary_line=_summary_line(records), symbol_lines=symbol_lines)
