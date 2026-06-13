"""Load optimized signal weights produced by the backtester."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any


DEFAULT_WEIGHTS_FILE = "signal_weights.json"
DEFAULT_MIN_ENTRY_CONFIDENCE = 65
DEFAULT_MIN_ENTRY_RR = 2.0
DEFENSIVE_MIN_ENTRY_CONFIDENCE = 70
DEFENSIVE_MIN_ENTRY_RR = 3.0


@dataclass(frozen=True)
class OptimizedSignalWeights:
    min_entry_confidence: int | None
    min_entry_rr: float | None
    symbol_adjustments: dict[str, int]
    symbol_notes: dict[str, str]

    def adjustment_for(self, symbol: str) -> int:
        return self.symbol_adjustments.get(symbol, 0)

    def note_for(self, symbol: str) -> str | None:
        return self.symbol_notes.get(symbol)


def _weights_path() -> Path:
    return Path(os.getenv("SIGNAL_WEIGHTS_FILE", DEFAULT_WEIGHTS_FILE))


def _as_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _as_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _unsafe_metrics(payload: dict[str, Any]) -> bool:
    metrics = payload.get("metrics")
    if not isinstance(metrics, dict):
        return False

    wins = _as_int(metrics.get("wins")) or 0
    losses = _as_int(metrics.get("losses")) or 0
    closed = wins + losses
    win_rate = _as_float(metrics.get("win_rate")) or 0.0
    total_return = _as_float(metrics.get("total_return_pct")) or 0.0
    return closed >= 5 and losses > wins and win_rate < 40 and total_return < 0


def _safe_thresholds(payload: dict[str, Any], optimized: dict[str, Any]) -> tuple[int | None, float | None]:
    min_confidence = _as_int(optimized.get("min_entry_confidence"))
    min_rr = _as_float(optimized.get("min_entry_rr"))
    if _unsafe_metrics(payload):
        min_confidence = max(min_confidence or DEFAULT_MIN_ENTRY_CONFIDENCE, DEFENSIVE_MIN_ENTRY_CONFIDENCE)
        min_rr = max(min_rr or DEFAULT_MIN_ENTRY_RR, DEFENSIVE_MIN_ENTRY_RR)
    return min_confidence, min_rr


def load_signal_weights() -> OptimizedSignalWeights:
    if os.getenv("SIGNAL_WEIGHTS_ENABLED", "true").lower() in {"0", "false", "no"}:
        return OptimizedSignalWeights(None, None, {}, {})

    path = _weights_path()
    if not path.exists():
        return OptimizedSignalWeights(None, None, {}, {})

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return OptimizedSignalWeights(None, None, {}, {})

    optimized = payload.get("optimized") if isinstance(payload, dict) else {}
    if not isinstance(optimized, dict):
        optimized = {}

    symbols = payload.get("symbols") if isinstance(payload, dict) else {}
    if not isinstance(symbols, dict):
        symbols = {}

    adjustments: dict[str, int] = {}
    notes: dict[str, str] = {}
    for symbol, raw in symbols.items():
        if not isinstance(symbol, str) or not isinstance(raw, dict):
            continue
        adjustment = _as_int(raw.get("confidence_adjustment"))
        if adjustment:
            adjustments[symbol.upper()] = max(-20, min(15, adjustment))
        note = raw.get("note")
        if isinstance(note, str) and note:
            notes[symbol.upper()] = f"Backtest: {note}"

    min_confidence, min_rr = _safe_thresholds(payload, optimized)
    return OptimizedSignalWeights(
        min_entry_confidence=min_confidence,
        min_entry_rr=min_rr,
        symbol_adjustments=adjustments,
        symbol_notes=notes,
    )
