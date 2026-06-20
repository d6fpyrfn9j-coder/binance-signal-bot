#!/usr/bin/env python3
"""Adaptive signal component weight optimizer.

The optimizer only consumes completed backtest/history records. It does not
fetch candles or recalculate indicators after a signal timestamp.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from signal_weights import (
    DEFAULT_COMPONENT_WEIGHT,
    MARKET_REGIMES,
    MAX_COMPONENT_WEIGHT,
    MIN_COMPONENT_WEIGHT,
    SIGNAL_COMPONENTS,
)


DEFAULT_BACKTEST_FILE = "backtest_results.json"
DEFAULT_SIGNAL_HISTORY_FILE = "signal_history.json"
DEFAULT_FUTURES_HISTORY_FILE = "futures_signal_history.json"
DEFAULT_OUTPUT_FILE = "optimized_weights.json"
MAX_WEIGHT_CHANGE_PCT = 20.0
UNKNOWN_PRIOR_WEIGHT = 0.35


@dataclass
class ComponentStats:
    samples: float = 0.0
    wins: float = 0.0
    losses: float = 0.0
    gross_profit: float = 0.0
    gross_loss: float = 0.0

    def add(self, pnl: float, strength: float = 1.0) -> None:
        strength = max(0.1, min(3.0, strength))
        self.samples += strength
        if pnl > 0:
            self.wins += strength
            self.gross_profit += pnl * strength
        elif pnl < 0:
            self.losses += strength
            self.gross_loss += abs(pnl) * strength

    def merge(self, other: "ComponentStats", weight: float = 1.0) -> None:
        self.samples += other.samples * weight
        self.wins += other.wins * weight
        self.losses += other.losses * weight
        self.gross_profit += other.gross_profit * weight
        self.gross_loss += other.gross_loss * weight

    @property
    def decisive_samples(self) -> float:
        return self.wins + self.losses

    @property
    def win_rate(self) -> float:
        total = self.decisive_samples
        return (self.wins / total) * 100 if total else 0.0

    @property
    def profit_factor(self) -> float | None:
        if self.gross_loss > 0:
            return self.gross_profit / self.gross_loss
        if self.gross_profit > 0:
            return None
        return 0.0


def _read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _as_float(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(parsed) or math.isinf(parsed):
        return None
    return parsed


def _normalize_regime(value: Any, period: str | None = None) -> str:
    raw = str(value or "").upper()
    if raw in MARKET_REGIMES:
        return raw

    period_text = (period or "").lower()
    if "bear" in period_text:
        return "BEAR"
    if "bull" in period_text:
        return "BULL"
    if "range" in period_text:
        return "RANGE"
    if "late_bull" in period_text or "late-bull" in period_text:
        return "LATE_BULL"
    return "UNKNOWN"


def _iter_backtest_events(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        return []

    events: list[dict[str, Any]] = []
    periods = payload.get("periods")
    if isinstance(periods, dict):
        for period_name, period_payload in periods.items():
            if not isinstance(period_payload, dict):
                continue
            for event in period_payload.get("events", []):
                if isinstance(event, dict):
                    cloned = dict(event)
                    cloned["_source"] = "backtest_results.json"
                    cloned["_period"] = str(period_name)
                    events.append(cloned)

    for key in ("events", "futures_events"):
        for event in payload.get(key, []):
            if isinstance(event, dict):
                cloned = dict(event)
                cloned["_source"] = f"backtest_results.json:{key}"
                events.append(cloned)
    return events


def _iter_history_events(payload: Any, source: str) -> list[dict[str, Any]]:
    if not isinstance(payload, list):
        return []
    events: list[dict[str, Any]] = []
    for event in payload:
        if isinstance(event, dict):
            cloned = dict(event)
            cloned["_source"] = source
            events.append(cloned)
    return events


def _is_completed(event: dict[str, Any]) -> bool:
    status = str(event.get("status") or event.get("outcome") or event.get("exit_reason") or "").lower()
    if status == "open":
        return False
    if event.get("closed_at") or event.get("closed_at_ms"):
        return True
    return status in {
        "win",
        "loss",
        "timeout",
        "take_profit",
        "stop_loss",
        "liquidation",
        "protected",
        "fake",
        "expired",
        "no_trigger",
    }


def _event_pnl(event: dict[str, Any]) -> float | None:
    status = str(event.get("status") or event.get("outcome") or event.get("exit_reason") or "").lower()
    if status in {"open", "no_trade", "risk_skip", "skip"}:
        return None

    net_pnl = _as_float(event.get("net_pnl"))
    quantity = _as_float(event.get("quantity"))
    if net_pnl is not None and (quantity is None or quantity > 0):
        if net_pnl != 0:
            return net_pnl

    return_pct = _as_float(event.get("return_pct"))
    if return_pct is not None and status not in {"protected", "expired", "no_trigger"}:
        if return_pct != 0:
            return return_pct

    if status in {"win", "take_profit"}:
        return max(return_pct or 0.25, 0.25)
    if status in {"loss", "stop_loss", "liquidation", "fake"}:
        return min(return_pct or -0.50, -0.25)
    if status == "protected":
        pct_1h = _as_float(event.get("pct_1h"))
        pct_4h = _as_float(event.get("pct_4h"))
        protected_move = min(value for value in (pct_1h, pct_4h, 0.0) if value is not None)
        return max(abs(protected_move), 0.10)
    return None


def _has_number(event: dict[str, Any], key: str) -> bool:
    return _as_float(event.get(key)) is not None


def _decision_text(event: dict[str, Any]) -> str:
    return " ".join(
        str(event.get(key) or "")
        for key in ("decision", "reason", "result", "exit_reason", "flow_crowding", "flow_smart_money_bias")
    ).lower()


def _component_strengths(event: dict[str, Any]) -> dict[str, float]:
    strengths: dict[str, float] = {}

    for raw_key in ("component_scores", "component_strengths", "features"):
        raw = event.get(raw_key)
        if not isinstance(raw, dict):
            continue
        for component in SIGNAL_COMPONENTS:
            value = _as_float(raw.get(component))
            if value is not None and value != 0:
                strengths[component] = max(abs(value), strengths.get(component, 0.0))

    text = _decision_text(event)
    if "rsi" in text:
        strengths["rsi"] = max(strengths.get("rsi", 0.0), 1.0)
    if any(token in text for token in ("ema", "trend", "yön", "yon", "zayif", "zayıf")):
        strengths["ema_trend"] = max(strengths.get("ema_trend", 0.0), 1.0)
    if "macd" in text:
        strengths["macd"] = max(strengths.get("macd", 0.0), 1.0)
    if any(token in text for token in ("hacim", "volume", "akış", "akis", "flow", "para")):
        strengths["volume"] = max(strengths.get("volume", 0.0), 1.0)
    if any(token in text for token in ("duvar", "order book", "orderbook", "büyük emir", "buyuk emir")):
        strengths["order_book"] = max(strengths.get("order_book", 0.0), 1.0)

    if _normalize_regime(event.get("market_regime"), event.get("_period")) != "UNKNOWN":
        strengths["market_regime"] = max(strengths.get("market_regime", 0.0), 1.0)
    if _has_number(event, "flow_oi_change_pct"):
        strengths["open_interest"] = max(strengths.get("open_interest", 0.0), 1.0)
    if _has_number(event, "flow_funding_rate_pct"):
        funding = abs(_as_float(event.get("flow_funding_rate_pct")) or 0.0)
        strengths["funding_rate"] = max(strengths.get("funding_rate", 0.0), min(2.0, max(0.5, funding / 0.01)))
    if _has_number(event, "flow_long_short_ratio") or str(event.get("flow_crowding") or "").upper() not in {"", "NEUTRAL", "UNKNOWN"}:
        strengths["long_short_ratio"] = max(strengths.get("long_short_ratio", 0.0), 1.0)

    if event.get("side") in {"LONG", "SHORT"} or event.get("bias") in {"LONG", "SHORT"}:
        strengths["ema_trend"] = max(strengths.get("ema_trend", 0.0), 0.75)
    if _as_float(event.get("confidence")) is not None:
        strengths["ema_trend"] = max(strengths.get("ema_trend", 0.0), 0.5)

    return strengths


def _empty_regime_stats() -> dict[str, dict[str, ComponentStats]]:
    return {
        regime: {component: ComponentStats() for component in SIGNAL_COMPONENTS}
        for regime in (*MARKET_REGIMES, "UNKNOWN")
    }


def collect_component_stats(events: list[dict[str, Any]]) -> dict[str, dict[str, ComponentStats]]:
    stats = _empty_regime_stats()
    for event in events:
        if not _is_completed(event):
            continue
        pnl = _event_pnl(event)
        if pnl is None or pnl == 0:
            continue
        regime = _normalize_regime(event.get("market_regime"), event.get("_period") or event.get("period"))
        strengths = _component_strengths(event)
        if not strengths:
            continue
        for component, strength in strengths.items():
            stats[regime][component].add(pnl, strength)
    return stats


def _previous_weights(path: Path) -> dict[str, dict[str, float]]:
    weights = {
        regime: {component: DEFAULT_COMPONENT_WEIGHT for component in SIGNAL_COMPONENTS}
        for regime in MARKET_REGIMES
    }
    payload = _read_json(path, {})
    regimes = payload.get("regimes") if isinstance(payload, dict) else {}
    if not isinstance(regimes, dict):
        return weights
    for regime in MARKET_REGIMES:
        raw = regimes.get(regime, {})
        raw_weights = raw.get("weights") if isinstance(raw, dict) else {}
        if not isinstance(raw_weights, dict):
            continue
        for component in SIGNAL_COMPONENTS:
            parsed = _as_float(raw_weights.get(component))
            if parsed is not None:
                weights[regime][component] = max(MIN_COMPONENT_WEIGHT, min(MAX_COMPONENT_WEIGHT, parsed))
    return weights


def _bounded_weight(previous: float, target: float) -> float:
    lower = max(MIN_COMPONENT_WEIGHT, previous * (1 - MAX_WEIGHT_CHANGE_PCT / 100))
    upper = min(MAX_COMPONENT_WEIGHT, previous * (1 + MAX_WEIGHT_CHANGE_PCT / 100))
    return round(max(lower, min(upper, target)), 4)


def _target_weight(previous: float, stats: ComponentStats, min_samples: float) -> float:
    if stats.decisive_samples < min_samples:
        return previous

    if stats.gross_loss == 0 and stats.gross_profit > 0:
        change = 0.12
    elif stats.gross_profit == 0 and stats.gross_loss > 0:
        change = -0.12
    else:
        profit_factor = stats.profit_factor or 0.0
        pf_edge = max(-1.0, min(1.0, profit_factor - 1.0))
        win_edge = max(-1.0, min(1.0, (stats.win_rate - 50.0) / 50.0))
        change = pf_edge * 0.12 + win_edge * 0.08

    change = max(-MAX_WEIGHT_CHANGE_PCT / 100, min(MAX_WEIGHT_CHANGE_PCT / 100, change))
    return _bounded_weight(previous, previous * (1 + change))


def _stats_payload(stats: ComponentStats, regime_profit: float, regime_loss: float) -> dict[str, Any]:
    profit_factor = stats.profit_factor
    return {
        "samples": round(stats.samples, 2),
        "wins": round(stats.wins, 2),
        "losses": round(stats.losses, 2),
        "win_rate": round(stats.win_rate, 2),
        "win_contribution": round((stats.gross_profit / regime_profit) * 100, 2) if regime_profit else 0.0,
        "loss_contribution": round((stats.gross_loss / regime_loss) * 100, 2) if regime_loss else 0.0,
        "profit_factor_contribution": round(profit_factor, 4) if profit_factor is not None else None,
        "gross_profit": round(stats.gross_profit, 4),
        "gross_loss": round(stats.gross_loss, 4),
    }


def optimize_weights(
    stats: dict[str, dict[str, ComponentStats]],
    previous: dict[str, dict[str, float]],
    min_samples: float,
) -> dict[str, dict[str, Any]]:
    output: dict[str, dict[str, Any]] = {}
    unknown_stats = stats["UNKNOWN"]

    for regime in MARKET_REGIMES:
        combined: dict[str, ComponentStats] = {}
        for component in SIGNAL_COMPONENTS:
            item = ComponentStats()
            item.merge(stats[regime][component])
            if item.decisive_samples < min_samples:
                item.merge(unknown_stats[component], UNKNOWN_PRIOR_WEIGHT)
            combined[component] = item

        regime_profit = sum(component.gross_profit for component in combined.values())
        regime_loss = sum(component.gross_loss for component in combined.values())
        weights: dict[str, float] = {}
        components: dict[str, Any] = {}
        for component, component_stats in combined.items():
            old_weight = previous[regime][component]
            new_weight = _target_weight(old_weight, component_stats, min_samples)
            weights[component] = max(MIN_COMPONENT_WEIGHT, new_weight)
            components[component] = _stats_payload(component_stats, regime_profit, regime_loss)
            components[component]["previous_weight"] = round(old_weight, 4)
            components[component]["new_weight"] = weights[component]

        output[regime] = {
            "weights": weights,
            "components": components,
        }
    return output


def build_payload(args: argparse.Namespace) -> dict[str, Any]:
    backtest_path = Path(args.backtest)
    signal_history_path = Path(args.signal_history)
    futures_history_path = Path(args.futures_history)
    output_path = Path(args.output)

    events = []
    events.extend(_iter_backtest_events(_read_json(backtest_path, {})))
    events.extend(_iter_history_events(_read_json(signal_history_path, []), "signal_history.json"))
    events.extend(_iter_history_events(_read_json(futures_history_path, []), "futures_signal_history.json"))

    stats = collect_component_stats(events)
    previous = _previous_weights(output_path)
    regimes = optimize_weights(stats, previous, args.min_samples)
    completed_events = [
        event
        for event in events
        if _is_completed(event) and _event_pnl(event) is not None and _component_strengths(event)
    ]

    return {
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "version": 1,
        "source_files": {
            "backtest": str(backtest_path),
            "signal_history": str(signal_history_path),
            "futures_history": str(futures_history_path),
        },
        "lookahead_guard": (
            "Uses only completed events and their recorded signal-time fields; "
            "does not fetch or recalculate future candles."
        ),
        "rules": {
            "min_weight": MIN_COMPONENT_WEIGHT,
            "default_weight": DEFAULT_COMPONENT_WEIGHT,
            "max_weight_change_pct_per_run": MAX_WEIGHT_CHANGE_PCT,
            "min_decisive_samples": args.min_samples,
            "unknown_regime_prior_weight": UNKNOWN_PRIOR_WEIGHT,
        },
        "events_used": len(completed_events),
        "components": list(SIGNAL_COMPONENTS),
        "regimes": regimes,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Optimize adaptive signal component weights")
    parser.add_argument("--backtest", default=DEFAULT_BACKTEST_FILE)
    parser.add_argument("--signal-history", default=DEFAULT_SIGNAL_HISTORY_FILE)
    parser.add_argument("--futures-history", default=DEFAULT_FUTURES_HISTORY_FILE)
    parser.add_argument("--output", default=DEFAULT_OUTPUT_FILE)
    parser.add_argument("--min-samples", type=float, default=3.0)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    payload = build_payload(args)
    output_path = Path(args.output)
    output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print("OPTIMIZER TAMAM")
    print(f"Kayit: {payload['events_used']} tamamlanmis event")
    print(f"Yazildi: {output_path}")
    for regime in MARKET_REGIMES:
        weights = payload["regimes"][regime]["weights"]
        changed = [
            f"{component}={weight:.2f}"
            for component, weight in weights.items()
            if abs(weight - DEFAULT_COMPONENT_WEIGHT) >= 0.001
        ]
        suffix = ", ".join(changed) if changed else "varsayilan"
        print(f"{regime}: {suffix}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
