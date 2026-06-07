"""Load optimized signal weights produced by the backtester."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any


DEFAULT_WEIGHTS_FILE = "signal_weights.json"


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

    return OptimizedSignalWeights(
        min_entry_confidence=_as_int(optimized.get("min_entry_confidence")),
        min_entry_rr=_as_float(optimized.get("min_entry_rr")),
        symbol_adjustments=adjustments,
        symbol_notes=notes,
    )
