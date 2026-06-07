"""Persist and score manual trade signals over time."""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from data_fetcher import Candle, fetch_binance_klines_range


MAX_HISTORY = 250
DEFAULT_HISTORY_FILE = "signal_history.json"
MAX_RECORDS_TO_AUDIT = int(os.getenv("MAX_SIGNAL_AUDIT_RECORDS", "30"))
MIN_TRACK_TRIGGER_CONFIDENCE = int(os.getenv("MIN_TRACK_TRIGGER_CONFIDENCE", "55"))
MIN_TRACK_TRIGGER_RR = float(os.getenv("MIN_TRACK_TRIGGER_RR", "2.0"))
MIN_TRACK_OBSERVE_CONFIDENCE = int(os.getenv("MIN_TRACK_OBSERVE_CONFIDENCE", "40"))
AUDIT_WINDOW_SECONDS = int(os.getenv("SIGNAL_AUDIT_WINDOW_SECONDS", "14400"))


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
    decision: str
    needs_trigger: bool


@dataclass(frozen=True)
class SignalTrackerResult:
    summary_line: str
    audit_line: str
    symbol_lines: dict[str, str]


@dataclass(frozen=True)
class SignalPerformanceProfile:
    symbol_adjustments: dict[str, int]
    symbol_notes: dict[str, str]
    summary_line: str | None = None

    def adjustment_for(self, symbol: str) -> int:
        return self.symbol_adjustments.get(symbol, 0)

    def note_for(self, symbol: str) -> str | None:
        return self.symbol_notes.get(symbol)


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


def _record_side(candidate: SignalCandidate) -> str:
    if candidate.active:
        return "AL"
    if _is_actionable_trigger(candidate):
        return "TETIK"
    return "IZLE"


def _has_hard_no_marker(decision: str) -> bool:
    text = decision.upper()
    hard_markers = (
        "HAYIR",
        "GİRİŞ YOK",
        "BTC ZAYIF",
        "TREND DÜŞÜŞTE",
        "SATIŞ BASKISI",
        "PARA ÇIKIŞI",
        "DESTEK KIRILDI",
        "HABER RİSKİ",
        "FAKE",
        "DAĞITIM",
        "R/R DÜŞÜK",
        "GÜVEN DÜŞÜK",
    )
    return any(marker in text for marker in hard_markers)


def _is_hard_no(candidate: SignalCandidate) -> bool:
    return _has_hard_no_marker(candidate.decision)


def _is_actionable_trigger(candidate: SignalCandidate) -> bool:
    return (
        candidate.needs_trigger
        and not _is_hard_no(candidate)
        and candidate.confidence >= MIN_TRACK_TRIGGER_CONFIDENCE
        and candidate.rr >= MIN_TRACK_TRIGGER_RR
    )


def _price_path(symbol: str, created_at: dt.datetime, now: dt.datetime) -> list[Candle]:
    start_ms = int(created_at.timestamp() * 1000)
    audit_end = min(now, created_at + dt.timedelta(seconds=AUDIT_WINDOW_SECONDS + 900))
    end_ms = int(audit_end.timestamp() * 1000)
    return fetch_binance_klines_range(symbol, "15m", start_ms, end_ms, limit=500)


def _pct_from_entry(entry: float, price: float) -> float | None:
    if entry <= 0:
        return None
    return ((price - entry) / entry) * 100


def _price_at_or_after(candles: list[Candle], created_at: dt.datetime, seconds: int) -> float | None:
    target_ms = int((created_at + dt.timedelta(seconds=seconds)).timestamp() * 1000)
    for candle in candles:
        if candle.close_time >= target_ms:
            return candle.close
    return None


def _close_record(record: dict[str, Any], status: str, result: str, closed_at_ms: int) -> None:
    if record.get("status") not in {"success", "failed", "protected", "missed", "fake"}:
        record["status"] = status
        record["result"] = result
        record["closed_at"] = dt.datetime.fromtimestamp(
            closed_at_ms / 1000,
            tz=dt.timezone.utc,
        ).isoformat()


def _audit_price_path(record: dict[str, Any], candles: list[Candle], now: dt.datetime) -> None:
    if not candles:
        return

    entry = float(record.get("entry") or 0)
    target = float(record.get("target") or 0)
    stop = float(record.get("stop") or 0)
    side = str(record.get("side") or "AL")
    created_at = _parse_time(str(record.get("created_at", "")))
    if entry <= 0 or target <= 0 or stop <= 0 or not created_at:
        return

    triggered = bool(record.get("triggered"))
    fake_touches = int(record.get("fake_touches") or 0)
    trigger_time_ms = int(record.get("trigger_time_ms") or 0)
    deadline_ms = int((created_at + dt.timedelta(seconds=AUDIT_WINDOW_SECONDS)).timestamp() * 1000)

    for candle in candles:
        if candle.open_time > deadline_ms:
            break

        if side == "AL":
            if candle.low <= stop:
                _close_record(record, "failed", "Stop", candle.close_time)
                break
            if candle.high >= target:
                _close_record(record, "success", "Hedef", candle.close_time)
                break
            continue

        if side == "TETIK":
            if not triggered:
                if candle.close_time > deadline_ms:
                    break
                if candle.close >= entry:
                    triggered = True
                    trigger_time_ms = candle.close_time
                    record["triggered"] = True
                    record["trigger_time_ms"] = trigger_time_ms
                    record["result"] = "Tetik 15m kapanış"
                    continue
                elif candle.high >= entry:
                    fake_touches += 1
                    record["fake_touches"] = fake_touches
                    if candle.low <= stop:
                        _close_record(record, "protected", "Fake tetik korundu", candle.close_time)
                        break

            if triggered:
                if trigger_time_ms and candle.close_time <= trigger_time_ms:
                    continue
                if candle.close_time > deadline_ms:
                    break
                # Conservative order: if stop and target happen in the same 15m candle, count risk first.
                if candle.low <= stop:
                    _close_record(record, "failed", "Tetik sonrası stop", candle.close_time)
                    break
                if candle.high >= target:
                    _close_record(record, "success", "Tetik sonrası hedef", candle.close_time)
                    break
            continue

        if side == "IZLE":
            if candle.close_time > deadline_ms:
                break
            if candle.low <= stop:
                _close_record(record, "protected", "İzle kararı zararı korudu", candle.close_time)
                break
            if candle.high >= target and candle.close >= entry:
                _close_record(record, "missed", "Kaçan fırsat", candle.close_time)
                break

    price_1h = _price_at_or_after(candles, created_at, 3600)
    price_4h = _price_at_or_after(candles, created_at, 14_400)
    if record.get("pct_1h") is None and price_1h is not None:
        record["pct_1h"] = _pct_from_entry(entry, price_1h)
    if record.get("pct_4h") is None and price_4h is not None:
        record["pct_4h"] = _pct_from_entry(entry, price_4h)

    elapsed = (now - created_at).total_seconds()
    if elapsed >= 14_400 and record.get("status") in {None, "open"}:
        pct_4h = record.get("pct_4h")
        if side in {"AL", "TETIK"} and triggered:
            record["status"] = "success" if isinstance(pct_4h, (int, float)) and pct_4h > 0 else "failed"
            record["result"] = "4s test"
            record["closed_at"] = now.isoformat()
        elif side == "TETIK":
            if fake_touches:
                record["status"] = "fake"
                record["result"] = "Fitil var, kapanış yok"
            else:
                record["status"] = "protected"
                record["result"] = "Tetik gelmedi"
            record["closed_at"] = now.isoformat()


def _normalize_legacy_records(records: list[dict[str, Any]]) -> None:
    for record in records:
        decision = str(record.get("decision") or "")
        side = str(record.get("side") or "")
        if side not in {"AL", "TETIK"} or not _has_hard_no_marker(decision):
            continue

        record["side"] = "IZLE"
        record["triggered"] = False
        record["trigger_time_ms"] = None
        record["pct_1h"] = None
        record["pct_4h"] = None
        if record.get("status") in {"success", "failed"}:
            record["status"] = "open"
            record["result"] = "Hard no yeniden izlendi"
            record.pop("closed_at", None)


def _update_record(record: dict[str, Any], candidate: SignalCandidate, now: dt.datetime) -> None:
    created_at = _parse_time(str(record.get("created_at", "")))
    if not created_at:
        return

    elapsed = (now - created_at).total_seconds()
    status = str(record.get("status") or "open")
    side = str(record.get("side") or "AL")
    if status == "open" and side == "AL":
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
        if side == "AL" and record.get("status") == "open" and pct_4h is not None:
            record["status"] = "success" if pct_4h > 0 else "failed"
            record["result"] = "4s test"
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
            (now - created_at).total_seconds() > 21_600
            and record.get("pct_4h") is not None
            and record.get("status") not in {None, "open"}
        ):
            continue
        try:
            candles = _price_path(symbol, created_at, now)
            _audit_price_path(record, candles, now)
        except Exception:
            logging.exception("Could not audit signal record for %s", symbol)


def _is_duplicate(record: dict[str, Any], candidate: SignalCandidate, now: dt.datetime) -> bool:
    side = _record_side(candidate)
    if record.get("symbol") != candidate.symbol or record.get("side") != side:
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
    side = _record_side(candidate)
    return {
        "symbol": candidate.symbol,
        "side": side,
        "decision": candidate.decision,
        "created_at": now.isoformat(),
        "entry": candidate.entry,
        "target": candidate.target,
        "stop": candidate.stop,
        "confidence": candidate.confidence,
        "rr": candidate.rr,
        "status": "open",
        "triggered": side == "AL",
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
    pct_1h = latest.get("pct_1h")
    pct_4h = latest.get("pct_4h")
    created_at = _parse_time(str(latest.get("created_at", "")))
    fresh = bool(created_at and (_now_utc() - created_at).total_seconds() < 600)
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
    elif fresh:
        side = str(latest.get("side") or "")
        suffix = "AL kaydı 🟢" if side == "AL" else "İzleniyor 🟡"
    else:
        suffix = "Açık 🟡"
    return f"Test: 1s {_fmt_pct(pct_1h)} | 4s {_fmt_pct(pct_4h)} | {suffix}"


def _summary_line(records: list[dict[str, Any]]) -> str:
    records_100 = [
        record
        for record in records[-100:]
        if record.get("side") in {"AL", "TETIK"} and record.get("status") in {"success", "failed"}
    ]
    protected = sum(1 for record in records[-100:] if record.get("status") in {"protected", "fake"})
    if not records_100:
        if protected:
            return f"Son 100 sinyal: veri birikiyor 🟡 | korunan {protected}"
        return "Son 100 sinyal: veri birikiyor 🟡"

    wins = sum(1 for record in records_100 if record.get("status") == "success")
    rate = (wins / len(records_100)) * 100
    emoji = "🟢" if rate >= 60 else "🟡" if rate >= 45 else "🔴"
    suffix = f" | korunan {protected}" if protected else ""
    return f"Son 100 sinyal: %{rate:.0f} {emoji} ({wins}/{len(records_100)}){suffix}"


def _audit_line(records: list[dict[str, Any]]) -> str:
    latest = records[-100:]
    missed = sum(1 for record in latest if record.get("status") == "missed")
    fake = sum(1 for record in latest if record.get("status") == "fake")
    protected = sum(1 for record in latest if record.get("status") == "protected")
    failed = sum(1 for record in latest if record.get("status") == "failed")
    if not any((missed, fake, protected, failed)):
        return "Hata testi: veri birikiyor 🟡"
    return f"Hata testi: kaçan {missed} | fake {fake} | korunan {protected} | stop {failed}"


def load_signal_performance_profile() -> SignalPerformanceProfile:
    if os.getenv("PERFORMANCE_LEARNING_ENABLED", "true").lower() in {"0", "false", "no"}:
        return SignalPerformanceProfile(symbol_adjustments={}, symbol_notes={})

    records = _load_records(_history_path())[-100:]
    _normalize_legacy_records(records)
    stats: dict[str, dict[str, int]] = {}
    for record in records:
        symbol = str(record.get("symbol") or "")
        status = str(record.get("status") or "")
        side = str(record.get("side") or "")
        if not symbol or status in {"", "open", "None"}:
            continue

        bucket = stats.setdefault(
            symbol,
            {
                "action": 0,
                "success": 0,
                "failed": 0,
                "fake": 0,
                "missed": 0,
                "protected": 0,
            },
        )
        if side in {"AL", "TETIK"}:
            bucket["action"] += 1
            if status == "success":
                bucket["success"] += 1
            elif status == "failed":
                bucket["failed"] += 1
            elif status == "fake":
                bucket["fake"] += 1
        elif side == "IZLE":
            if status == "missed":
                bucket["missed"] += 1
            elif status in {"protected", "fake"}:
                bucket["protected"] += 1

    adjustments: dict[str, int] = {}
    notes: dict[str, str] = {}
    total_missed = 0
    total_failed = 0
    for symbol, bucket in stats.items():
        adjustment = 0
        action = bucket["action"]
        success = bucket["success"]
        failed = bucket["failed"]
        missed = bucket["missed"]
        fake = bucket["fake"]
        total_missed += missed
        total_failed += failed

        if action >= 2:
            win_rate = success / action if action else 0.0
            if failed >= 2 and win_rate < 0.40:
                adjustment -= 10
                notes[symbol] = "Öğrenme: geçmiş stop zayıf 🔴"
            elif success >= 2 and win_rate >= 0.65:
                adjustment += 5
                notes[symbol] = "Öğrenme: geçmiş sinyal iyi 🟢"

        if missed >= 2:
            adjustment += 6
            notes[symbol] = "Öğrenme: geçmişte fırsat kaçtı 🟡"
        elif missed == 1 and action == 0:
            adjustment += 3

        if fake >= 2:
            adjustment -= 6
            notes[symbol] = "Öğrenme: fake riski geçmişte yüksek 🔴"

        if adjustment:
            adjustments[symbol] = max(-15, min(10, adjustment))

    summary = None
    if total_missed or total_failed:
        summary = f"Öğrenme: kaçan {total_missed} | stop {total_failed}"

    return SignalPerformanceProfile(
        symbol_adjustments=adjustments,
        symbol_notes=notes,
        summary_line=summary,
    )


def track_signals(candidates: list[SignalCandidate]) -> SignalTrackerResult:
    if os.getenv("SIGNAL_TRACKING_ENABLED", "true").lower() in {"0", "false", "no"}:
        return SignalTrackerResult(summary_line="Son 100 sinyal: kapalı", audit_line="Hata testi: kapalı", symbol_lines={})

    path = _history_path()
    records = _load_records(path)
    now = _now_utc()
    _normalize_legacy_records(records)
    candidate_map = {candidate.symbol: candidate for candidate in candidates}

    _audit_records_with_binance(records, now)
    for record in records:
        symbol = str(record.get("symbol") or "")
        candidate = candidate_map.get(symbol)
        if candidate:
            _update_record(record, candidate, now)

    for candidate in candidates:
        side = _record_side(candidate)
        if side in {"AL", "TETIK"} and candidate.rr < MIN_TRACK_TRIGGER_RR:
            continue
        if side == "IZLE" and candidate.confidence < MIN_TRACK_OBSERVE_CONFIDENCE:
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
    return SignalTrackerResult(
        summary_line=_summary_line(records),
        audit_line=_audit_line(records),
        symbol_lines=symbol_lines,
    )
