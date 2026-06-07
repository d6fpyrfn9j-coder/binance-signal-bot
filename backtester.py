#!/usr/bin/env python3
"""Historical Binance backtest and automatic signal-weight optimizer."""

from __future__ import annotations

import argparse
import bisect
import datetime as dt
import json
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from analyzer import (
    _btc_4h_bearish,
    _clamp,
    _confidence_score,
    _entry_decision,
    _quality_adjustment,
    _risk_reward_ratio,
    _trade_setup_values,
    analyze_symbol,
)
from data_fetcher import Candle, MarketStat, fetch_binance_klines_range
from main import ALL_SYMBOLS


DEFAULT_SYMBOLS = (
    "BTCUSDT",
    "ETHUSDT",
    "SOLUSDT",
    "BNBUSDT",
    "XRPUSDT",
    "SUIUSDT",
    "NEARUSDT",
    "ONDOUSDT",
    "LINKUSDT",
    "FETUSDT",
)
INTERVAL_MS = {
    "15m": 15 * 60 * 1000,
    "1h": 60 * 60 * 1000,
    "4h": 4 * 60 * 60 * 1000,
}
MIN_OPTIMIZER_TRADES = 8


@dataclass(frozen=True)
class SignalEvent:
    symbol: str
    opened_at: str
    decision: str
    entry_allowed: bool
    confidence: int
    rr: float
    entry: float
    target: float
    stop: float
    outcome: str
    return_pct: float
    missed: bool


@dataclass(frozen=True)
class BacktestMetrics:
    trades: int
    wins: int
    losses: int
    win_rate: float
    avg_return_pct: float
    total_return_pct: float
    max_loss_pct: float
    missed: int
    protected: int


def _utc_ms(value: dt.datetime) -> int:
    return int(value.timestamp() * 1000)


def _iso_from_ms(value: int) -> str:
    return dt.datetime.fromtimestamp(value / 1000, tz=dt.timezone.utc).isoformat()


def _fetch_klines_paginated(symbol: str, interval: str, start_ms: int, end_ms: int) -> list[Candle]:
    candles: list[Candle] = []
    cursor = start_ms
    while cursor < end_ms:
        chunk = fetch_binance_klines_range(symbol, interval, cursor, end_ms, limit=1000)
        if not chunk:
            break
        if candles:
            chunk = [candle for candle in chunk if candle.open_time > candles[-1].open_time]
        candles.extend(chunk)
        next_cursor = candles[-1].close_time + 1
        if next_cursor <= cursor:
            break
        cursor = next_cursor
        time.sleep(0.05)
    return candles


def _close_times(candles: list[Candle]) -> list[int]:
    return [candle.close_time for candle in candles]


def _window(candles: list[Candle], close_times: list[int], close_time: int, size: int = 250) -> list[Candle]:
    index = bisect.bisect_right(close_times, close_time)
    if index < size:
        return []
    return candles[index - size:index]


def _future(candles: list[Candle], close_times: list[int], close_time: int, bars: int = 16) -> list[Candle]:
    index = bisect.bisect_right(close_times, close_time)
    return candles[index:index + bars]


def _market_stat(symbol: str, candle: Candle, previous: Candle | None = None) -> MarketStat:
    previous_close = previous.close if previous else candle.open
    change_pct = ((candle.close - previous_close) / previous_close) * 100 if previous_close else 0.0
    quote_volume = candle.quote_volume or candle.close * candle.volume
    taker_buy = candle.taker_buy_quote_volume
    taker_sell = max(quote_volume - taker_buy, 0.0)
    return MarketStat(
        symbol=symbol,
        price_change_pct=change_pct,
        quote_volume=quote_volume,
        last_price=candle.close,
        taker_buy_quote_volume=taker_buy,
        taker_sell_quote_volume=taker_sell,
        net_taker_quote_volume=taker_buy - taker_sell,
    )


def _market_24h_stat(symbol: str, candles: list[Candle]) -> MarketStat:
    recent = candles[-96:] if len(candles) >= 96 else candles
    first = recent[0]
    last = recent[-1]
    quote_volume = sum(candle.quote_volume or candle.close * candle.volume for candle in recent)
    change_pct = ((last.close - first.open) / first.open) * 100 if first.open else 0.0
    return MarketStat(symbol=symbol, price_change_pct=change_pct, quote_volume=quote_volume, last_price=last.close)


def _simulate(future: list[Candle], entry: float, target: float, stop: float, needs_trigger: bool) -> tuple[str, float]:
    if not future:
        return "no_data", 0.0

    triggered = not needs_trigger
    last_close = future[-1].close
    for candle in future:
        if not triggered:
            if candle.close >= entry:
                triggered = True
                continue
            continue

        if candle.low <= stop:
            return "loss", ((stop - entry) / entry) * 100 if entry else 0.0
        if candle.high >= target:
            return "win", ((target - entry) / entry) * 100 if entry else 0.0

    if not triggered:
        return "no_trigger", 0.0
    return "timeout", ((last_close - entry) / entry) * 100 if entry else 0.0


def _missed_or_protected(future: list[Candle], entry: float, target: float, stop: float) -> tuple[str, bool]:
    for candle in future:
        if candle.low <= stop:
            return "protected", False
        if candle.high >= target and candle.close >= entry:
            return "missed", True
    return "neutral", False


def _btc_cache_builder(btc_data: dict[str, list[Candle]]):
    close_times = {tf: _close_times(candles) for tf, candles in btc_data.items()}
    cache: dict[int, bool] = {}

    def is_bearish(close_time: int) -> bool:
        if close_time in cache:
            return cache[close_time]
        windows = {
            timeframe: _window(btc_data[timeframe], close_times[timeframe], close_time)
            for timeframe in ("15m", "1h", "4h")
        }
        if any(not candles for candles in windows.values()):
            cache[close_time] = True
            return True
        analysis = analyze_symbol("BTCUSDT", windows)
        cache[close_time] = _btc_4h_bearish([analysis])
        return cache[close_time]

    return is_bearish


def _collect_symbol_events(
    symbol: str,
    candles_by_timeframe: dict[str, list[Candle]],
    btc_is_bearish,
    start_ms: int,
    sample_every: int,
) -> list[SignalEvent]:
    close_times = {tf: _close_times(candles) for tf, candles in candles_by_timeframe.items()}
    candles_15m = candles_by_timeframe["15m"]
    events: list[SignalEvent] = []

    for index, candle in enumerate(candles_15m):
        if candle.close_time < start_ms or index % sample_every:
            continue

        windows = {
            timeframe: _window(candles_by_timeframe[timeframe], close_times[timeframe], candle.close_time)
            for timeframe in ("15m", "1h", "4h")
        }
        if any(not candles for candles in windows.values()):
            continue

        analysis = analyze_symbol(symbol, windows)
        altcoin_blocked = symbol != "BTCUSDT" and btc_is_bearish(candle.close_time)
        previous_15m = candles_15m[index - 1] if index > 0 else None
        flow_stats = {symbol: _market_stat(symbol, windows["15m"][-1], previous_15m)}
        confirm_stats = {symbol: _market_stat(symbol, windows["1h"][-1], windows["1h"][-2])}
        market_stats = {symbol: _market_24h_stat(symbol, candles_15m[max(0, index - 96):index + 1])}

        entry_line, entry_allowed = _entry_decision(analysis, altcoin_blocked, None, flow_stats)
        setup = _trade_setup_values(analysis, entry_allowed)
        if not setup:
            continue

        rr = _risk_reward_ratio(setup) or 0.0
        confidence = _confidence_score(
            analysis,
            entry_allowed,
            altcoin_blocked,
            None,
            flow_stats,
            confirm_stats,
            None,
            None,
            None,
            rr,
        )
        quality_adjust, _ = _quality_adjustment(symbol, market_stats, flow_stats, None)
        confidence = _clamp(confidence + quality_adjust)

        entry, target, stop, _, needs_trigger = setup
        future = _future(candles_15m, close_times["15m"], candle.close_time)
        if not future:
            continue

        if entry_allowed:
            outcome, return_pct = _simulate(future, entry, target, stop, needs_trigger)
            missed = False
        else:
            outcome, missed = _missed_or_protected(future, entry, target, stop)
            return_pct = 0.0

        events.append(
            SignalEvent(
                symbol=symbol,
                opened_at=_iso_from_ms(candle.close_time),
                decision=entry_line,
                entry_allowed=entry_allowed,
                confidence=confidence,
                rr=rr,
                entry=entry,
                target=target,
                stop=stop,
                outcome=outcome,
                return_pct=return_pct,
                missed=missed,
            )
        )
    return events


def _metrics(events: list[SignalEvent], min_confidence: int, min_rr: float) -> BacktestMetrics:
    trades = [
        event
        for event in events
        if event.entry_allowed and event.confidence >= min_confidence and event.rr >= min_rr
    ]
    wins = sum(1 for event in trades if event.outcome == "win")
    losses = sum(1 for event in trades if event.outcome == "loss")
    closed = wins + losses
    win_rate = (wins / closed) * 100 if closed else 0.0
    returns = [event.return_pct for event in trades if event.outcome in {"win", "loss", "timeout"}]
    avg_return = sum(returns) / len(returns) if returns else 0.0
    total_return = sum(returns)
    max_loss = min(returns) if returns else 0.0
    missed = sum(1 for event in events if event.missed)
    protected = sum(1 for event in events if event.outcome == "protected")
    return BacktestMetrics(
        trades=len(trades),
        wins=wins,
        losses=losses,
        win_rate=win_rate,
        avg_return_pct=avg_return,
        total_return_pct=total_return,
        max_loss_pct=max_loss,
        missed=missed,
        protected=protected,
    )


def _score(metrics: BacktestMetrics) -> float:
    trade_penalty = max(0, 5 - metrics.trades) * 0.5
    loss_penalty = metrics.losses * 0.7
    return metrics.total_return_pct + metrics.win_rate * 0.08 - abs(metrics.max_loss_pct) * 0.5 - trade_penalty - loss_penalty


def _optimize(events: list[SignalEvent]) -> tuple[dict[str, float], BacktestMetrics]:
    default_settings = {"min_entry_confidence": 65, "min_entry_rr": 2.0}
    default_metrics = _metrics(events, 65, 2.0)
    best_settings = dict(default_settings)
    best_metrics = default_metrics
    best_score = _score(best_metrics)

    for min_confidence in (55, 60, 65, 70, 75):
        for min_rr in (1.5, 2.0, 2.5, 3.0):
            metrics = _metrics(events, min_confidence, min_rr)
            score = _score(metrics)
            if score > best_score:
                best_score = score
                best_settings = {"min_entry_confidence": min_confidence, "min_entry_rr": min_rr}
                best_metrics = metrics
    if (
        best_metrics.trades < MIN_OPTIMIZER_TRADES
        and (
            best_settings["min_entry_confidence"] < default_settings["min_entry_confidence"]
            or best_settings["min_entry_rr"] < default_settings["min_entry_rr"]
        )
    ):
        return default_settings, default_metrics
    return best_settings, best_metrics


def _symbol_weights(events: list[SignalEvent], min_confidence: int, min_rr: float) -> dict[str, dict[str, object]]:
    symbols = sorted({event.symbol for event in events})
    payload: dict[str, dict[str, object]] = {}
    for symbol in symbols:
        symbol_events = [event for event in events if event.symbol == symbol]
        trades = [
            event
            for event in symbol_events
            if event.entry_allowed and event.confidence >= min_confidence and event.rr >= min_rr
        ]
        wins = sum(1 for event in trades if event.outcome == "win")
        losses = sum(1 for event in trades if event.outcome == "loss")
        missed = sum(1 for event in symbol_events if event.missed)
        protected = sum(1 for event in symbol_events if event.outcome == "protected")
        closed = wins + losses
        win_rate = (wins / closed) if closed else 0.0

        adjustment = 0
        note = ""
        if closed >= 3 and win_rate < 0.40:
            adjustment -= 10
            note = "geçmiş başarı zayıf 🔴"
        elif closed >= 3 and win_rate >= 0.65:
            adjustment += 5
            note = "geçmiş başarı iyi 🟢"
        opportunity_watch = missed >= 3 and closed <= 2
        if opportunity_watch:
            adjustment += 8
            note = "Fırsat radarı 🟡"
        if protected >= 5 and wins == 0:
            adjustment -= 2 if opportunity_watch else 4
            if not opportunity_watch:
                note = "koruma baskın, temkinli 🔴"

        payload[symbol] = {
            "confidence_adjustment": max(-15, min(10, adjustment)),
            "trades": len(trades),
            "wins": wins,
            "losses": losses,
            "win_rate": round(win_rate * 100, 1),
            "missed": missed,
            "protected": protected,
            "note": note,
        }
    return payload


def run_backtest(symbols: tuple[str, ...], days: int, sample_every: int) -> dict[str, object]:
    now = dt.datetime.now(dt.timezone.utc)
    warmup_days = 50
    start = now - dt.timedelta(days=days)
    fetch_start = start - dt.timedelta(days=warmup_days)
    start_ms = _utc_ms(start)
    fetch_start_ms = _utc_ms(fetch_start)
    end_ms = _utc_ms(now)

    fetch_symbols = tuple(dict.fromkeys(("BTCUSDT", *symbols)))
    market_data: dict[str, dict[str, list[Candle]]] = {}
    for symbol in fetch_symbols:
        market_data[symbol] = {
            timeframe: _fetch_klines_paginated(symbol, timeframe, fetch_start_ms, end_ms)
            for timeframe in ("15m", "1h", "4h")
        }

    btc_is_bearish = _btc_cache_builder(market_data["BTCUSDT"])
    events: list[SignalEvent] = []
    for symbol in symbols:
        events.extend(_collect_symbol_events(symbol, market_data[symbol], btc_is_bearish, start_ms, sample_every))

    optimized, metrics = _optimize(events)
    symbol_weights = _symbol_weights(
        events,
        int(optimized["min_entry_confidence"]),
        float(optimized["min_entry_rr"]),
    )
    return {
        "generated_at": now.isoformat(),
        "days": days,
        "sample_every_15m": sample_every,
        "symbols_tested": list(symbols),
        "optimized": optimized,
        "metrics": asdict(metrics),
        "symbols": symbol_weights,
        "events": [asdict(event) for event in events[-500:]],
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Backtest Binance signal logic and optimize weights")
    parser.add_argument("--days", type=int, default=30, help="Backtest period in days")
    parser.add_argument("--sample-every", type=int, default=4, help="Evaluate every N 15m candles")
    parser.add_argument("--all-symbols", action="store_true", help="Backtest every configured symbol")
    parser.add_argument("--symbols", nargs="*", default=None, help="Symbols to backtest, e.g. BTCUSDT ETHUSDT SOLUSDT")
    parser.add_argument("--output", default="backtest_results.json", help="Where to write full backtest output")
    parser.add_argument("--weights-output", default="signal_weights.json", help="Where to write optimized bot weights")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.all_symbols:
        symbols = ALL_SYMBOLS
    elif args.symbols:
        symbols = tuple(symbol.upper() for symbol in args.symbols)
    else:
        symbols = DEFAULT_SYMBOLS

    result = run_backtest(symbols=symbols, days=args.days, sample_every=max(args.sample_every, 1))
    Path(args.output).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    weights = {key: result[key] for key in ("generated_at", "days", "sample_every_15m", "symbols_tested", "optimized", "metrics", "symbols")}
    Path(args.weights_output).write_text(json.dumps(weights, ensure_ascii=False, indent=2), encoding="utf-8")

    metrics = result["metrics"]
    optimized = result["optimized"]
    print("BACKTEST TAMAM")
    print(f"Sembol: {', '.join(symbols)}")
    print(f"En iyi güven: {optimized['min_entry_confidence']} | R/R: {optimized['min_entry_rr']}")
    print(
        "Sonuç: "
        f"trade {metrics['trades']} | win {metrics['wins']} | loss {metrics['losses']} | "
        f"başarı %{metrics['win_rate']:.1f} | toplam {metrics['total_return_pct']:+.2f}% | "
        f"kaçan {metrics['missed']} | korunan {metrics['protected']}"
    )
    print(f"Yazıldı: {args.output}, {args.weights_output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
