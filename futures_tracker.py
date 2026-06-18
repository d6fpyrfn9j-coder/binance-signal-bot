"""Persist and score futures direction signals over time."""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from data_fetcher import Candle, fetch_binance_klines_range


MAX_HISTORY = 300
DEFAULT_HISTORY_FILE = "futures_signal_history.json"
MAX_RECORDS_TO_AUDIT = int(os.getenv("MAX_FUTURES_AUDIT_RECORDS", "40"))
MIN_TRACK_CONFIDENCE = int(os.getenv("MIN_FUTURES_TRACK_CONFIDENCE", "60"))
AUDIT_WINDOW_SECONDS = int(os.getenv("FUTURES_AUDIT_WINDOW_SECONDS", "14400"))


@dataclass(frozen=True)
class FuturesSignalCandidate:
    symbol: str
    side: str
    bias: str
    entry: float | None
    target: float | None
    stop: float | None
    current_price: float
    confidence: int
    rr: float
    decision: str
    needs_trigger: bool


@dataclass(frozen=True)
class FuturesTrackerResult:
    summary_line: str
    audit_line: str
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
    configured = os.getenv("FUTURES_SIGNAL_HISTORY_FILE", DEFAULT_HISTORY_FILE)
    return Path(configured)


def _load_records(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        logging.exception("Could not read futures signal history")
        return []
    if isinstance(payload, list):
        return [record for record in payload if isinstance(record, dict)]
    return []


def _save_records(path: Path, records: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(records[-MAX_HISTORY:], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _record_side(candidate: FuturesSignalCandidate) -> str:
    if candidate.side in {"LONG", "SHORT"}:
        return candidate.side
    if candidate.bias in {"LONG", "SHORT"}:
        return candidate.bias
    return "BEKLE"


def _record_action(candidate: FuturesSignalCandidate) -> str:
    return "TRADE" if candidate.side in {"LONG", "SHORT"} else "OBSERVE"


def _price_path(symbol: str, created_at: dt.datetime, now: dt.datetime) -> list[Candle]:
    start_ms = int(created_at.timestamp() * 1000)
    audit_end = min(now, created_at + dt.timedelta(seconds=AUDIT_WINDOW_SECONDS + 900))
    end_ms = int(audit_end.timestamp() * 1000)
    return fetch_binance_klines_range(symbol, "15m", start_ms, end_ms, limit=500)


def _pct_from_entry(side: str, entry: float, price: float) -> float | None:
    if entry <= 0:
        return None
    if side == "SHORT":
        return ((entry - price) / entry) * 100
    return ((price - entry) / entry) * 100


def _price_at_or_after(candles: list[Candle], created_at: dt.datetime, seconds: int) -> float | None:
    target_ms = int((created_at + dt.timedelta(seconds=seconds)).timestamp() * 1000)
    for candle in candles:
        if candle.close_time >= target_ms:
            return candle.close
    return None


def _close_record(record: dict[str, Any], status: str, result: str, closed_at_ms: int) -> None:
    if record.get("status") not in {"success", "failed", "protected", "missed", "fake", "expired"}:
        record["status"] = status
        record["result"] = result
        record["closed_at"] = dt.datetime.fromtimestamp(
            closed_at_ms / 1000,
            tz=dt.timezone.utc,
        ).isoformat()


def _triggered(side: str, candle: Candle, entry: float) -> bool:
    if side == "SHORT":
        return candle.close <= entry
    return candle.close >= entry


def _touched(side: str, candle: Candle, entry: float) -> bool:
    if side == "SHORT":
        return candle.low <= entry
    return candle.high >= entry


def _hit_target(side: str, candle: Candle, target: float) -> bool:
    if side == "SHORT":
        return candle.low <= target
    return candle.high >= target


def _hit_stop(side: str, candle: Candle, stop: float) -> bool:
    if side == "SHORT":
        return candle.high >= stop
    return candle.low <= stop


def _audit_trade(record: dict[str, Any], candles: list[Candle], created_at: dt.datetime) -> None:
    entry = float(record.get("entry") or 0)
    target = float(record.get("target") or 0)
    stop = float(record.get("stop") or 0)
    side = str(record.get("side") or "")
    if side not in {"LONG", "SHORT"} or entry <= 0 or target <= 0 or stop <= 0:
        return

    triggered = bool(record.get("triggered"))
    needs_trigger = bool(record.get("needs_trigger"))
    fake_touches = int(record.get("fake_touches") or 0)
    trigger_time_ms = int(record.get("trigger_time_ms") or 0)
    deadline_ms = int((created_at + dt.timedelta(seconds=AUDIT_WINDOW_SECONDS)).timestamp() * 1000)

    for candle in candles:
        if candle.open_time > deadline_ms:
            break

        if needs_trigger and not triggered:
            if _triggered(side, candle, entry):
                triggered = True
                trigger_time_ms = candle.close_time
                record["triggered"] = True
                record["trigger_time_ms"] = trigger_time_ms
                record["result"] = "Tetik 15m kapanış"
                continue
            if _touched(side, candle, entry):
                fake_touches += 1
                record["fake_touches"] = fake_touches
            continue

        if triggered:
            if trigger_time_ms and candle.close_time <= trigger_time_ms:
                continue
            if _hit_stop(side, candle, stop):
                _close_record(record, "failed", "Stop", candle.close_time)
                break
            if _hit_target(side, candle, target):
                _close_record(record, "success", "Hedef", candle.close_time)
                break


def _audit_observe(record: dict[str, Any], candles: list[Candle], created_at: dt.datetime) -> None:
    entry = float(record.get("entry") or 0)
    target = float(record.get("target") or 0)
    stop = float(record.get("stop") or 0)
    side = str(record.get("side") or "")
    if side not in {"LONG", "SHORT"} or entry <= 0 or target <= 0 or stop <= 0:
        return

    triggered = False
    deadline_ms = int((created_at + dt.timedelta(seconds=AUDIT_WINDOW_SECONDS)).timestamp() * 1000)
    for candle in candles:
        if candle.open_time > deadline_ms:
            break
        if not triggered:
            if _triggered(side, candle, entry):
                triggered = True
            elif _touched(side, candle, entry):
                record["fake_touches"] = int(record.get("fake_touches") or 0) + 1
            continue

        if _hit_stop(side, candle, stop):
            _close_record(record, "protected", "Bekle kararı zararı korudu", candle.close_time)
            break
        if _hit_target(side, candle, target):
            _close_record(record, "missed", "Kaçan futures fırsatı", candle.close_time)
            break


def _audit_price_path(record: dict[str, Any], candles: list[Candle], now: dt.datetime) -> None:
    if not candles:
        return

    created_at = _parse_time(str(record.get("created_at", "")))
    if not created_at:
        return
    action = str(record.get("action") or "TRADE")
    side = str(record.get("side") or "")
    entry = float(record.get("entry") or 0)

    if action == "TRADE":
        _audit_trade(record, candles, created_at)
    else:
        _audit_observe(record, candles, created_at)

    price_1h = _price_at_or_after(candles, created_at, 3600)
    price_4h = _price_at_or_after(candles, created_at, 14_400)
    if record.get("pct_1h") is None and price_1h is not None:
        record["pct_1h"] = _pct_from_entry(side, entry, price_1h)
    if record.get("pct_4h") is None and price_4h is not None:
        record["pct_4h"] = _pct_from_entry(side, entry, price_4h)

    elapsed = (now - created_at).total_seconds()
    if elapsed >= AUDIT_WINDOW_SECONDS and record.get("status") in {None, "open"}:
        if action == "TRADE" and record.get("triggered"):
            pct_4h = record.get("pct_4h")
            record["status"] = "success" if isinstance(pct_4h, (int, float)) and pct_4h > 0 else "failed"
            record["result"] = "4s test"
        elif int(record.get("fake_touches") or 0) > 0:
            record["status"] = "fake"
            record["result"] = "Fitil var, kapanış yok"
        else:
            record["status"] = "expired"
            record["result"] = "Tetik gelmedi"
        record["closed_at"] = now.isoformat()


def _audit_records_with_binance(records: list[dict[str, Any]], now: dt.datetime) -> None:
    candidates = [
        record
        for record in records
        if record.get("status") in {None, "open"}
        or record.get("pct_1h") is None
        or record.get("pct_4h") is None
    ]
    for record in candidates[-MAX_RECORDS_TO_AUDIT:]:
        created_at = _parse_time(str(record.get("created_at", "")))
        symbol = str(record.get("symbol") or "")
        if not created_at or not symbol:
            continue
        if (
            (now - created_at).total_seconds() > AUDIT_WINDOW_SECONDS + 7200
            and record.get("pct_4h") is not None
            and record.get("status") not in {None, "open"}
        ):
            continue
        try:
            candles = _price_path(symbol, created_at, now)
            _audit_price_path(record, candles, now)
        except Exception:
            logging.exception("Could not audit futures signal record for %s", symbol)


def _is_duplicate(record: dict[str, Any], candidate: FuturesSignalCandidate, now: dt.datetime) -> bool:
    side = _record_side(candidate)
    action = _record_action(candidate)
    if record.get("symbol") != candidate.symbol or record.get("side") != side or record.get("action") != action:
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
    entry = candidate.entry or 0
    if old_entry <= 0 or entry <= 0:
        return False
    return abs(entry - old_entry) / old_entry <= 0.005


def _new_record(candidate: FuturesSignalCandidate, now: dt.datetime) -> dict[str, Any]:
    side = _record_side(candidate)
    action = _record_action(candidate)
    return {
        "symbol": candidate.symbol,
        "side": side,
        "action": action,
        "decision": candidate.decision,
        "created_at": now.isoformat(),
        "entry": candidate.entry,
        "target": candidate.target,
        "stop": candidate.stop,
        "confidence": candidate.confidence,
        "rr": candidate.rr,
        "status": "open",
        "needs_trigger": candidate.needs_trigger,
        "triggered": action == "TRADE" and not candidate.needs_trigger,
        "trigger_time_ms": None,
        "fake_touches": 0,
        "pct_1h": None,
        "pct_4h": None,
        "result": None,
    }


def _latest_symbol_line(records: list[dict[str, Any]], symbol: str) -> str | None:
    latest = next((record for record in reversed(records) if record.get("symbol") == symbol), None)
    if not latest:
        return None

    status = str(latest.get("status") or "open")
    side = str(latest.get("side") or "")
    action = str(latest.get("action") or "")
    pct_1h = latest.get("pct_1h")
    pct_4h = latest.get("pct_4h")
    if status == "success":
        suffix = "Başarılı ✅"
    elif status == "failed":
        suffix = "Başarısız 🔴"
    elif status == "protected":
        suffix = "Korundu 🟢"
    elif status == "missed":
        suffix = "Kaçan fırsat 🔴"
    elif status == "fake":
        suffix = "Fake yakalandı 🟢"
    elif status == "expired":
        suffix = "Tetik gelmedi 🟡"
    elif action == "TRADE":
        suffix = f"{side} açık 🟡"
    else:
        suffix = f"{side} izleniyor 🟡"
    return f"Futures Test: 1s {_fmt_pct(pct_1h)} | 4s {_fmt_pct(pct_4h)} | {suffix}"


def _summary_line(records: list[dict[str, Any]]) -> str:
    trades = [
        record
        for record in records[-100:]
        if record.get("action") == "TRADE" and record.get("status") in {"success", "failed"}
    ]
    if not trades:
        return "Futures son 100: veri birikiyor 🟡"
    wins = sum(1 for record in trades if record.get("status") == "success")
    rate = (wins / len(trades)) * 100
    emoji = "🟢" if rate >= 55 else "🟡" if rate >= 42 else "🔴"
    return f"Futures son 100: %{rate:.0f} {emoji} ({wins}/{len(trades)})"


def _audit_line(records: list[dict[str, Any]]) -> str:
    latest = records[-100:]
    missed = sum(1 for record in latest if record.get("status") == "missed")
    fake = sum(1 for record in latest if record.get("status") == "fake")
    protected = sum(1 for record in latest if record.get("status") == "protected")
    failed = sum(1 for record in latest if record.get("status") == "failed")
    if not any((missed, fake, protected, failed)):
        return "Futures hata testi: veri birikiyor 🟡"
    return f"Futures hata testi: kaçan {missed} | fake {fake} | korunan {protected} | stop {failed}"


def track_futures_signals(candidates: list[FuturesSignalCandidate]) -> FuturesTrackerResult:
    if os.getenv("FUTURES_TRACKING_ENABLED", "true").lower() in {"0", "false", "no"}:
        return FuturesTrackerResult(
            summary_line="Futures son 100: kapalı",
            audit_line="Futures hata testi: kapalı",
            symbol_lines={},
        )

    path = _history_path()
    records = _load_records(path)
    now = _now_utc()

    _audit_records_with_binance(records, now)

    for candidate in candidates:
        side = _record_side(candidate)
        if side not in {"LONG", "SHORT"}:
            continue
        if candidate.entry is None or candidate.target is None or candidate.stop is None:
            continue
        if candidate.confidence < MIN_TRACK_CONFIDENCE:
            continue
        if any(_is_duplicate(record, candidate, now) for record in records):
            continue
        records.append(_new_record(candidate, now))

    try:
        _save_records(path, records)
    except Exception:
        logging.exception("Could not save futures signal history")

    symbol_lines = {
        candidate.symbol: line
        for candidate in candidates
        if (line := _latest_symbol_line(records, candidate.symbol))
    }
    return FuturesTrackerResult(
        summary_line=_summary_line(records),
        audit_line=_audit_line(records),
        symbol_lines=symbol_lines,
    )
