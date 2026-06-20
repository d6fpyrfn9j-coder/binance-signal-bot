"""Load optimized signal weights produced by the backtester."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any


DEFAULT_WEIGHTS_FILE = "signal_weights.json"
DEFAULT_OPTIMIZED_WEIGHTS_FILE = "optimized_weights.json"
DEFAULT_MIN_ENTRY_CONFIDENCE = 65
DEFAULT_MIN_ENTRY_RR = 2.0
DEFENSIVE_MIN_ENTRY_CONFIDENCE = 70
DEFENSIVE_MIN_ENTRY_RR = 3.0
DEFAULT_COMPONENT_WEIGHT = 1.0
MIN_COMPONENT_WEIGHT = 0.2
MAX_COMPONENT_WEIGHT = 3.0
MARKET_REGIMES = ("BULL", "BEAR", "RANGE", "LATE_BULL")
SIGNAL_COMPONENTS = (
    "rsi",
    "ema_trend",
    "macd",
    "volume",
    "market_regime",
    "open_interest",
    "funding_rate",
    "long_short_ratio",
    "order_book",
)


@dataclass(frozen=True)
class OptimizedSignalWeights:
    min_entry_confidence: int | None
    min_entry_rr: float | None
    symbol_adjustments: dict[str, int]
    symbol_notes: dict[str, str]
    component_weights: dict[str, dict[str, float]]

    def adjustment_for(self, symbol: str) -> int:
        return self.symbol_adjustments.get(symbol, 0)

    def note_for(self, symbol: str) -> str | None:
        return self.symbol_notes.get(symbol)

    def component_weight(self, component: str, market_regime: str | None = None) -> float:
        component = component.strip().lower()
        if component not in SIGNAL_COMPONENTS:
            return DEFAULT_COMPONENT_WEIGHT
        regime = _normalize_regime(market_regime)
        weights = self.component_weights.get(regime, {})
        return weights.get(component, DEFAULT_COMPONENT_WEIGHT)


def _weights_path() -> Path:
    return Path(os.getenv("SIGNAL_WEIGHTS_FILE", DEFAULT_WEIGHTS_FILE))


def _optimized_weights_path() -> Path:
    return Path(os.getenv("OPTIMIZED_WEIGHTS_FILE", DEFAULT_OPTIMIZED_WEIGHTS_FILE))


def _normalize_regime(value: str | None) -> str:
    if not value:
        return "RANGE"
    normalized = str(value).upper()
    return normalized if normalized in MARKET_REGIMES else "RANGE"


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


def _component_weight_payload() -> dict[str, dict[str, float]]:
    weights = {
        regime: {component: DEFAULT_COMPONENT_WEIGHT for component in SIGNAL_COMPONENTS}
        for regime in MARKET_REGIMES
    }
    if os.getenv("OPTIMIZED_WEIGHTS_ENABLED", "true").lower() in {"0", "false", "no"}:
        return weights

    path = _optimized_weights_path()
    if not path.exists():
        return weights

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return weights

    regimes = payload.get("regimes") if isinstance(payload, dict) else {}
    if not isinstance(regimes, dict):
        return weights

    for regime, raw_regime in regimes.items():
        regime_key = _normalize_regime(regime)
        if not isinstance(raw_regime, dict):
            continue
        raw_weights = raw_regime.get("weights")
        if not isinstance(raw_weights, dict):
            continue
        for component in SIGNAL_COMPONENTS:
            parsed = _as_float(raw_weights.get(component))
            if parsed is None:
                continue
            weights[regime_key][component] = max(
                MIN_COMPONENT_WEIGHT,
                min(MAX_COMPONENT_WEIGHT, parsed),
            )
    return weights


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
        return OptimizedSignalWeights(None, None, {}, {}, _component_weight_payload())

    path = _weights_path()
    if not path.exists():
        return OptimizedSignalWeights(None, None, {}, {}, _component_weight_payload())

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return OptimizedSignalWeights(None, None, {}, {}, _component_weight_payload())

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
        component_weights=_component_weight_payload(),
    )
