#!/usr/bin/env python3
"""Historical Binance backtest and automatic signal-weight optimizer."""

from __future__ import annotations

import argparse
import bisect
import datetime as dt
import json
import logging
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path

from analyzer import (
    FUTURES_MAX_LEVERAGE,
    FUTURES_MAX_RISK_PCT,
    FUTURES_MAX_STOP_LOSS_PCT,
    FUTURES_MIN_RR,
    _btc_4h_bearish,
    _clamp,
    _confidence_score,
    _entry_decision,
    _futures_decision_line,
    _futures_signal,
    _market_regime_from_analyses,
    _quality_adjustment,
    _risk_reward_ratio,
    _trade_setup_values,
    analyze_symbol,
)
from data_fetcher import Candle, MarketStat, fetch_binance_klines_range
from futures_flow import (
    FuturesFlowHistory,
    FuturesFlowSnapshot,
    fetch_futures_flow_history,
    futures_flow_at,
)
from main import ALL_SYMBOLS
from market_regime import MarketRegime
from market_regime import MarketRegimeResult
from risk_engine import RiskPlan, RiskStatus, calculate_futures_risk


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
    "1d": 24 * 60 * 60 * 1000,
}
MIN_OPTIMIZER_TRADES = 8
DEFAULT_STARTING_BALANCE = 500.0
DEFAULT_FEE_RATE_PCT = 0.04
DEFAULT_SLIPPAGE_PCT = 0.05
BINANCE_FUTURES_BASE_URL = os.getenv("BINANCE_FUTURES_BASE_URL", "https://fapi.binance.com")
FUTURES_BACKTEST_LIMIT = 1500


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
class FuturesBacktestEvent:
    symbol: str
    opened_at: str
    side: str
    bias: str
    market_regime: str | None
    decision: str
    confidence: int
    risk_status: str
    risk_pct: float
    risk_amount: float
    daily_loss_pct: float
    daily_losing_trades: int
    rr: float
    entry: float
    target: float
    stop: float
    outcome: str
    return_pct: float
    needs_trigger: bool
    flow_oi_change_pct: float | None = None
    flow_funding_rate_pct: float | None = None
    flow_long_short_ratio: float | None = None
    flow_crowding: str | None = None
    flow_smart_money_bias: str | None = None


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


@dataclass(frozen=True)
class MarketCycle:
    name: str
    start: dt.datetime
    end: dt.datetime


@dataclass(frozen=True)
class ProfessionalFuturesEvent:
    period: str
    symbol: str
    opened_at: str
    closed_at: str | None
    side: str
    bias: str
    market_regime: str
    confidence: int
    risk_status: str
    risk_pct: float
    risk_amount: float
    rr: float
    entry: float
    entry_fill: float | None
    target: float
    stop: float
    exit_price: float | None
    exit_reason: str
    quantity: float
    notional_value: float
    fees: float
    gross_pnl: float
    net_pnl: float
    return_pct: float
    balance_before: float
    balance_after: float
    daily_loss_pct_before: float
    daily_losing_trades_before: int
    flow_oi_change_pct: float | None = None
    flow_funding_rate_pct: float | None = None
    flow_long_short_ratio: float | None = None
    flow_crowding: str | None = None
    flow_smart_money_bias: str | None = None


@dataclass(frozen=True)
class ProfessionalSummary:
    total_trades: int
    wins: int
    losses: int
    win_rate: float
    profit_factor: float | None
    max_drawdown_pct: float
    biggest_losing_streak: int
    average_win: float
    average_loss: float
    final_balance: float
    net_profit: float
    total_fees: float
    no_trade: int
    risk_skips: int
    best_performing_market_regime: dict[str, object] | None


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


def _read_json_url(url: str, timeout: int = 30):
    request = urllib.request.Request(url, headers={"User-Agent": "binance-futures-backtester/1.0"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Network error: {exc.reason}") from exc


def _parse_klines(rows) -> list[Candle]:
    candles: list[Candle] = []
    for row in rows:
        candles.append(
            Candle(
                open_time=int(row[0]),
                open=float(row[1]),
                high=float(row[2]),
                low=float(row[3]),
                close=float(row[4]),
                volume=float(row[5]),
                close_time=int(row[6]),
                quote_volume=float(row[7]),
                trade_count=int(row[8]),
                taker_buy_base_volume=float(row[9]),
                taker_buy_quote_volume=float(row[10]),
            )
        )
    return candles


def fetch_binance_futures_klines_range(
    symbol: str,
    interval: str,
    start_time_ms: int,
    end_time_ms: int | None = None,
    limit: int = FUTURES_BACKTEST_LIMIT,
) -> list[Candle]:
    params = {
        "symbol": symbol.upper(),
        "interval": interval,
        "startTime": start_time_ms,
        "limit": min(limit, FUTURES_BACKTEST_LIMIT),
    }
    if end_time_ms is not None:
        params["endTime"] = end_time_ms
    url = f"{BINANCE_FUTURES_BASE_URL.rstrip('/')}/fapi/v1/klines?{urllib.parse.urlencode(params)}"
    rows = _read_json_url(url)
    return _parse_klines(rows)


def _fetch_futures_klines_paginated(
    symbol: str,
    interval: str,
    start_ms: int,
    end_ms: int,
) -> list[Candle]:
    candles: list[Candle] = []
    cursor = start_ms
    while cursor < end_ms:
        chunk = fetch_binance_futures_klines_range(symbol, interval, cursor, end_ms)
        if not chunk:
            break
        if candles:
            chunk = [candle for candle in chunk if candle.open_time > candles[-1].open_time]
        candles.extend(chunk)
        next_cursor = candles[-1].close_time + 1
        if next_cursor <= cursor:
            break
        cursor = next_cursor
        time.sleep(0.03)
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


def _fetch_futures_flow_histories(
    symbols: tuple[str, ...],
    start_ms: int,
    end_ms: int,
) -> dict[str, FuturesFlowHistory]:
    histories: dict[str, FuturesFlowHistory] = {}
    for symbol in symbols:
        try:
            history = fetch_futures_flow_history(symbol, start_ms, end_ms)
        except Exception as exc:
            logging.warning("Skipping %s futures flow history: %s", symbol, exc)
            continue
        if any((history.open_interest, history.funding, history.global_ratio, history.top_ratio)):
            histories[symbol] = history
    return histories


def _analysis_futures_flow(
    symbol: str,
    analysis,
    history: FuturesFlowHistory | None,
    close_time: int,
) -> FuturesFlowSnapshot | None:
    if history is None:
        return None
    frames = {item.timeframe: item for item in analysis.timeframes}
    item_15m = frames.get("15m")
    return futures_flow_at(
        history,
        close_time,
        item_15m.change_pct if item_15m else 0.0,
        item_15m.volume_change_pct if item_15m else 0.0,
    )


def _flow_event_fields(flow: FuturesFlowSnapshot | None) -> dict[str, object]:
    return {
        "flow_oi_change_pct": flow.oi_change_pct if flow else None,
        "flow_funding_rate_pct": flow.funding_rate_pct if flow else None,
        "flow_long_short_ratio": flow.global_long_short_ratio if flow else None,
        "flow_crowding": flow.crowding if flow else None,
        "flow_smart_money_bias": flow.smart_money_bias if flow else None,
    }


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


def _simulate_futures(
    future: list[Candle],
    side: str,
    entry: float,
    target: float,
    stop: float,
    needs_trigger: bool,
) -> tuple[str, float]:
    if not future:
        return "no_data", 0.0

    triggered = not needs_trigger
    last_close = future[-1].close
    for candle in future:
        if not triggered:
            if side == "LONG" and candle.close >= entry:
                triggered = True
                continue
            if side == "SHORT" and candle.close <= entry:
                triggered = True
                continue
            continue

        if side == "LONG":
            if candle.low <= stop:
                return "loss", ((stop - entry) / entry) * 100 if entry else 0.0
            if candle.high >= target:
                return "win", ((target - entry) / entry) * 100 if entry else 0.0
        elif side == "SHORT":
            if candle.high >= stop:
                return "loss", ((entry - stop) / entry) * 100 if entry else 0.0
            if candle.low <= target:
                return "win", ((entry - target) / entry) * 100 if entry else 0.0

    if not triggered:
        return "no_trigger", 0.0
    if side == "SHORT":
        return "timeout", ((entry - last_close) / entry) * 100 if entry else 0.0
    return "timeout", ((last_close - entry) / entry) * 100 if entry else 0.0


def _apply_entry_slippage(side: str, price: float, slippage_pct: float) -> float:
    slippage = slippage_pct / 100
    if side == "SHORT":
        return price * (1 - slippage)
    return price * (1 + slippage)


def _apply_exit_slippage(side: str, price: float, slippage_pct: float) -> float:
    slippage = slippage_pct / 100
    if side == "SHORT":
        return price * (1 + slippage)
    return price * (1 - slippage)


def _professional_trade_execution(
    future: list[Candle],
    side: str,
    entry: float,
    target: float,
    stop: float,
    needs_trigger: bool,
    risk_plan: RiskPlan,
    fee_rate_pct: float,
    slippage_pct: float,
) -> dict[str, object] | None:
    if not future or risk_plan.position_size <= 0:
        return None

    quantity = risk_plan.position_size
    triggered = not needs_trigger
    entry_fill: float | None = None
    opened_at_ms: int | None = None
    exit_price: float | None = None
    exit_reason = "timeout"
    closed_at_ms = future[-1].close_time

    for candle in future:
        if not triggered:
            touched = candle.high >= entry if side == "LONG" else candle.low <= entry
            if not touched:
                continue
            triggered = True
            entry_fill = _apply_entry_slippage(side, entry, slippage_pct)
            opened_at_ms = candle.open_time
        elif entry_fill is None:
            entry_fill = _apply_entry_slippage(side, candle.open, slippage_pct)
            opened_at_ms = candle.open_time

        hit_stop = candle.low <= stop if side == "LONG" else candle.high >= stop
        hit_target = candle.high >= target if side == "LONG" else candle.low <= target
        if hit_stop:
            exit_price = _apply_exit_slippage(side, stop, slippage_pct)
            exit_reason = "loss"
            closed_at_ms = candle.close_time
            break
        if hit_target:
            exit_price = _apply_exit_slippage(side, target, slippage_pct)
            exit_reason = "win"
            closed_at_ms = candle.close_time
            break

    if not triggered or entry_fill is None:
        return None

    if exit_price is None:
        exit_price = _apply_exit_slippage(side, future[-1].close, slippage_pct)
        closed_at_ms = future[-1].close_time
        exit_reason = "timeout"

    if side == "SHORT":
        gross_pnl = (entry_fill - exit_price) * quantity
    else:
        gross_pnl = (exit_price - entry_fill) * quantity

    fee_rate = fee_rate_pct / 100
    fees = ((entry_fill * quantity) + (exit_price * quantity)) * fee_rate
    net_pnl = gross_pnl - fees
    return {
        "opened_at_ms": opened_at_ms,
        "closed_at_ms": closed_at_ms,
        "entry_fill": entry_fill,
        "exit_price": exit_price,
        "exit_reason": exit_reason,
        "quantity": quantity,
        "notional_value": entry_fill * quantity,
        "fees": fees,
        "gross_pnl": gross_pnl,
        "net_pnl": net_pnl,
    }


def _account_return_pct(risk_plan: RiskPlan | None, outcome: str, price_return_pct: float) -> float:
    if not risk_plan or risk_plan.account_balance <= 0:
        return price_return_pct
    if outcome == "loss":
        return -risk_plan.risk_pct
    exposure = risk_plan.notional_value / risk_plan.account_balance
    return price_return_pct * exposure


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


def _btc_regime_cache_builder(btc_data: dict[str, list[Candle]]):
    close_times = {tf: _close_times(candles) for tf, candles in btc_data.items()}
    cache: dict[int, MarketRegimeResult | None] = {}

    def regime_at(close_time: int) -> MarketRegimeResult | None:
        if close_time in cache:
            return cache[close_time]

        windows = {
            timeframe: _window(btc_data[timeframe], close_times[timeframe], close_time)
            for timeframe in ("15m", "1h", "4h")
            if timeframe in btc_data
        }
        if any(not candles for candles in windows.values()) or len(windows) < 3:
            cache[close_time] = None
            return None

        daily_window = (
            _window(btc_data["1d"], close_times["1d"], close_time, size=250)
            if "1d" in btc_data
            else []
        )
        if daily_window:
            windows["1d"] = daily_window

        analysis = analyze_symbol("BTCUSDT", windows)
        cache[close_time] = _market_regime_from_analyses([analysis], None)
        return cache[close_time]

    return regime_at


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


def _collect_symbol_futures_events(
    symbol: str,
    candles_by_timeframe: dict[str, list[Candle]],
    btc_is_bearish,
    btc_regime_at,
    start_ms: int,
    sample_every: int,
    flow_history: FuturesFlowHistory | None = None,
) -> list[FuturesBacktestEvent]:
    close_times = {tf: _close_times(candles) for tf, candles in candles_by_timeframe.items()}
    candles_15m = candles_by_timeframe["15m"]
    events: list[FuturesBacktestEvent] = []

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
        market_regime = btc_regime_at(candle.close_time) if btc_regime_at else None
        futures_flow = _analysis_futures_flow(symbol, analysis, flow_history, candle.close_time)
        signal = _futures_signal(
            analysis,
            altcoin_blocked,
            None,
            flow_stats,
            confirm_stats,
            None,
            None,
            None,
            market_regime,
            futures_flow=futures_flow,
        )
        if signal.side not in {"LONG", "SHORT"}:
            continue
        if signal.entry is None or signal.target is None or signal.stop is None:
            continue

        future = _future(candles_15m, close_times["15m"], candle.close_time)
        if not future:
            continue

        outcome, price_return_pct = _simulate_futures(
            future,
            signal.side,
            signal.entry,
            signal.target,
            signal.stop,
            signal.needs_trigger,
        )
        risk_plan = signal.risk_plan
        return_pct = _account_return_pct(risk_plan, outcome, price_return_pct)
        events.append(
            FuturesBacktestEvent(
                symbol=symbol,
                opened_at=_iso_from_ms(candle.close_time),
                side=signal.side,
                bias=signal.bias,
                market_regime=market_regime.regime.value if market_regime else None,
                decision=_futures_decision_line(signal),
                confidence=signal.confidence,
                risk_status=risk_plan.status.value if risk_plan else "",
                risk_pct=risk_plan.risk_pct if risk_plan else 0.0,
                risk_amount=risk_plan.risk_amount if risk_plan else 0.0,
                daily_loss_pct=risk_plan.daily_loss_pct if risk_plan else 0.0,
                daily_losing_trades=risk_plan.daily_losing_trades if risk_plan else 0,
                rr=signal.rr or 0.0,
                entry=signal.entry,
                target=signal.target,
                stop=signal.stop,
                outcome=outcome,
                return_pct=return_pct,
                needs_trigger=signal.needs_trigger,
                **_flow_event_fields(futures_flow),
            )
        )
    return events


def _collect_futures_events_chronological(
    symbols: tuple[str, ...],
    market_data: dict[str, dict[str, list[Candle]]],
    btc_is_bearish,
    btc_regime_at,
    start_ms: int,
    sample_every: int,
    flow_histories: dict[str, FuturesFlowHistory] | None = None,
) -> list[FuturesBacktestEvent]:
    scheduled: list[tuple[int, str, int]] = []
    for symbol in symbols:
        candles_15m = market_data[symbol]["15m"]
        for index, candle in enumerate(candles_15m):
            if candle.close_time >= start_ms and index % sample_every == 0:
                scheduled.append((candle.close_time, symbol, index))

    close_times_by_symbol = {
        symbol: {
            timeframe: _close_times(candles)
            for timeframe, candles in market_data[symbol].items()
        }
        for symbol in symbols
    }
    scheduled.sort(key=lambda item: item[0])

    events: list[FuturesBacktestEvent] = []
    active_day: dt.date | None = None
    daily_loss_pct = 0.0
    daily_losing_trades = 0

    for close_time, symbol, index in scheduled:
        event_day = dt.datetime.fromtimestamp(close_time / 1000, tz=dt.timezone.utc).date()
        if event_day != active_day:
            active_day = event_day
            daily_loss_pct = 0.0
            daily_losing_trades = 0

        candles_by_timeframe = market_data[symbol]
        close_times = close_times_by_symbol[symbol]
        candles_15m = candles_by_timeframe["15m"]
        candle = candles_15m[index]
        windows = {
            timeframe: _window(candles_by_timeframe[timeframe], close_times[timeframe], close_time)
            for timeframe in ("15m", "1h", "4h")
        }
        if any(not candles for candles in windows.values()):
            continue

        analysis = analyze_symbol(symbol, windows)
        altcoin_blocked = symbol != "BTCUSDT" and btc_is_bearish(close_time)
        previous_15m = candles_15m[index - 1] if index > 0 else None
        flow_stats = {symbol: _market_stat(symbol, windows["15m"][-1], previous_15m)}
        confirm_stats = {symbol: _market_stat(symbol, windows["1h"][-1], windows["1h"][-2])}
        market_regime = btc_regime_at(close_time) if btc_regime_at else None
        futures_flow = _analysis_futures_flow(
            symbol,
            analysis,
            flow_histories.get(symbol) if flow_histories else None,
            close_time,
        )
        signal = _futures_signal(
            analysis,
            altcoin_blocked,
            None,
            flow_stats,
            confirm_stats,
            None,
            None,
            None,
            market_regime,
            daily_loss_pct,
            daily_losing_trades,
            futures_flow,
        )
        risk_plan = signal.risk_plan
        if signal.entry is None or signal.target is None or signal.stop is None:
            continue

        if signal.side not in {"LONG", "SHORT"}:
            if risk_plan and risk_plan.status in {RiskStatus.SKIP, RiskStatus.NO_TRADE}:
                events.append(
                    FuturesBacktestEvent(
                        symbol=symbol,
                        opened_at=_iso_from_ms(close_time),
                        side="NO_TRADE" if risk_plan.status == RiskStatus.NO_TRADE else "SKIP",
                        bias=signal.bias,
                        market_regime=market_regime.regime.value if market_regime else None,
                        decision=_futures_decision_line(signal),
                        confidence=signal.confidence,
                        risk_status=risk_plan.status.value,
                        risk_pct=risk_plan.risk_pct,
                        risk_amount=risk_plan.risk_amount,
                        daily_loss_pct=risk_plan.daily_loss_pct,
                        daily_losing_trades=risk_plan.daily_losing_trades,
                        rr=signal.rr or 0.0,
                        entry=signal.entry,
                        target=signal.target,
                        stop=signal.stop,
                        outcome="no_trade" if risk_plan.status == RiskStatus.NO_TRADE else "risk_skip",
                        return_pct=0.0,
                        needs_trigger=signal.needs_trigger,
                        **_flow_event_fields(futures_flow),
                    )
                )
            continue

        future = _future(candles_15m, close_times["15m"], close_time)
        if not future:
            continue

        outcome, price_return_pct = _simulate_futures(
            future,
            signal.side,
            signal.entry,
            signal.target,
            signal.stop,
            signal.needs_trigger,
        )
        return_pct = _account_return_pct(risk_plan, outcome, price_return_pct)
        if outcome == "loss" or (outcome == "timeout" and return_pct < 0):
            daily_losing_trades += 1
            daily_loss_pct += abs(return_pct)

        events.append(
            FuturesBacktestEvent(
                symbol=symbol,
                opened_at=_iso_from_ms(close_time),
                side=signal.side,
                bias=signal.bias,
                market_regime=market_regime.regime.value if market_regime else None,
                decision=_futures_decision_line(signal),
                confidence=signal.confidence,
                risk_status=risk_plan.status.value if risk_plan else "",
                risk_pct=risk_plan.risk_pct if risk_plan else 0.0,
                risk_amount=risk_plan.risk_amount if risk_plan else 0.0,
                daily_loss_pct=risk_plan.daily_loss_pct if risk_plan else daily_loss_pct,
                daily_losing_trades=(
                    risk_plan.daily_losing_trades
                    if risk_plan
                    else daily_losing_trades
                ),
                rr=signal.rr or 0.0,
                entry=signal.entry,
                target=signal.target,
                stop=signal.stop,
                outcome=outcome,
                return_pct=return_pct,
                needs_trigger=signal.needs_trigger,
                **_flow_event_fields(futures_flow),
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


def _futures_metrics(events: list[FuturesBacktestEvent]) -> BacktestMetrics:
    trades = [
        event
        for event in events
        if event.outcome not in {"risk_skip", "no_trade"}
    ]
    wins = sum(1 for event in trades if event.outcome == "win")
    losses = sum(1 for event in trades if event.outcome == "loss")
    closed = wins + losses
    win_rate = (wins / closed) * 100 if closed else 0.0
    returns = [event.return_pct for event in trades if event.outcome in {"win", "loss", "timeout"}]
    avg_return = sum(returns) / len(returns) if returns else 0.0
    total_return = sum(returns)
    max_loss = min(returns) if returns else 0.0
    protected = sum(1 for event in events if event.outcome == "no_trade")
    return BacktestMetrics(
        trades=len(trades),
        wins=wins,
        losses=losses,
        win_rate=win_rate,
        avg_return_pct=avg_return,
        total_return_pct=total_return,
        max_loss_pct=max_loss,
        missed=0,
        protected=protected,
    )


def _default_market_cycles(now: dt.datetime | None = None) -> tuple[MarketCycle, ...]:
    now = now or dt.datetime.now(dt.timezone.utc)
    return (
        MarketCycle(
            "2021_bull_market",
            dt.datetime(2021, 1, 1, tzinfo=dt.timezone.utc),
            dt.datetime(2021, 11, 10, 23, 59, tzinfo=dt.timezone.utc),
        ),
        MarketCycle(
            "2022_bear_market",
            dt.datetime(2022, 1, 1, tzinfo=dt.timezone.utc),
            dt.datetime(2022, 12, 31, 23, 59, tzinfo=dt.timezone.utc),
        ),
        MarketCycle(
            "2024_bull_market",
            dt.datetime(2024, 1, 1, tzinfo=dt.timezone.utc),
            dt.datetime(2024, 12, 31, 23, 59, tzinfo=dt.timezone.utc),
        ),
        MarketCycle(
            "last_180_days",
            now - dt.timedelta(days=180),
            now,
        ),
    )


def _fetch_professional_period_data(
    symbols: tuple[str, ...],
    cycle: MarketCycle,
    warmup_days: int = 50,
    regime_warmup_days: int = 320,
) -> dict[str, dict[str, list[Candle]]]:
    fetch_start_ms = _utc_ms(cycle.start - dt.timedelta(days=warmup_days))
    regime_fetch_start_ms = _utc_ms(cycle.start - dt.timedelta(days=regime_warmup_days))
    end_ms = _utc_ms(cycle.end)
    market_data: dict[str, dict[str, list[Candle]]] = {}

    fetch_symbols = tuple(dict.fromkeys(("BTCUSDT", *symbols)))
    for symbol in fetch_symbols:
        try:
            frames = {
                timeframe: _fetch_futures_klines_paginated(symbol, timeframe, fetch_start_ms, end_ms)
                for timeframe in ("15m", "1h", "4h")
            }
            if symbol == "BTCUSDT":
                frames["1d"] = _fetch_futures_klines_paginated(
                    symbol,
                    "1d",
                    regime_fetch_start_ms,
                    end_ms,
                )
            if all(frames.get(timeframe) for timeframe in ("15m", "1h", "4h")):
                market_data[symbol] = frames
        except Exception as exc:
            logging.warning("Skipping %s for %s futures backtest: %s", symbol, cycle.name, exc)
    return market_data


def _professional_risk_plan(
    signal,
    side: str,
    market_regime: MarketRegimeResult | None,
    balance: float,
    daily_loss_pct: float,
    daily_losing_trades: int,
) -> RiskPlan:
    max_leverage = FUTURES_MAX_LEVERAGE
    if market_regime and market_regime.regime == MarketRegime.LATE_BULL:
        max_leverage = min(max_leverage, 3)
    min_rr = max(FUTURES_MIN_RR, market_regime.min_rr if market_regime else FUTURES_MIN_RR)
    return calculate_futures_risk(
        side=side,
        entry=float(signal.entry),
        stop_loss=float(signal.stop),
        take_profit=float(signal.target),
        account_balance=balance,
        risk_pct=FUTURES_MAX_RISK_PCT,
        confidence=signal.confidence,
        max_leverage=max_leverage,
        min_rr=min_rr,
        max_stop_loss_pct=FUTURES_MAX_STOP_LOSS_PCT,
        daily_loss_pct=daily_loss_pct,
        daily_losing_trades=daily_losing_trades,
    )


def _skip_event(
    cycle_name: str,
    symbol: str,
    close_time: int,
    signal,
    risk_plan: RiskPlan,
    market_regime: MarketRegimeResult | None,
    balance: float,
    daily_loss_pct: float,
    daily_losing_trades: int,
    futures_flow: FuturesFlowSnapshot | None = None,
) -> ProfessionalFuturesEvent:
    status = risk_plan.status.value
    return ProfessionalFuturesEvent(
        period=cycle_name,
        symbol=symbol,
        opened_at=_iso_from_ms(close_time),
        closed_at=None,
        side="NO_TRADE" if risk_plan.status == RiskStatus.NO_TRADE else "SKIP",
        bias=signal.bias,
        market_regime=market_regime.regime.value if market_regime else "UNKNOWN",
        confidence=signal.confidence,
        risk_status=status,
        risk_pct=risk_plan.risk_pct,
        risk_amount=risk_plan.risk_amount,
        rr=risk_plan.rr,
        entry=float(signal.entry or 0),
        entry_fill=None,
        target=float(signal.target or 0),
        stop=float(signal.stop or 0),
        exit_price=None,
        exit_reason=status,
        quantity=0.0,
        notional_value=0.0,
        fees=0.0,
        gross_pnl=0.0,
        net_pnl=0.0,
        return_pct=0.0,
        balance_before=balance,
        balance_after=balance,
        daily_loss_pct_before=daily_loss_pct,
        daily_losing_trades_before=daily_losing_trades,
        **_flow_event_fields(futures_flow),
    )


def _run_professional_cycle(
    cycle: MarketCycle,
    symbols: tuple[str, ...],
    sample_every: int,
    starting_balance: float,
    fee_rate_pct: float,
    slippage_pct: float,
) -> tuple[ProfessionalSummary, list[ProfessionalFuturesEvent]]:
    market_data = _fetch_professional_period_data(symbols, cycle)
    if "BTCUSDT" not in market_data:
        return _professional_summary([], starting_balance), []

    available_symbols = tuple(symbol for symbol in symbols if symbol in market_data)
    btc_is_bearish = _btc_cache_builder(market_data["BTCUSDT"])
    btc_regime_at = _btc_regime_cache_builder(market_data["BTCUSDT"])
    start_ms = _utc_ms(cycle.start)
    end_ms = _utc_ms(cycle.end)
    flow_histories = _fetch_futures_flow_histories(available_symbols, start_ms, end_ms)

    scheduled: list[tuple[int, str, int]] = []
    close_times_by_symbol = {
        symbol: {
            timeframe: _close_times(candles)
            for timeframe, candles in market_data[symbol].items()
        }
        for symbol in available_symbols
    }
    for symbol in available_symbols:
        for index, candle in enumerate(market_data[symbol]["15m"]):
            if candle.close_time >= start_ms and index % sample_every == 0:
                scheduled.append((candle.close_time, symbol, index))
    scheduled.sort(key=lambda item: item[0])

    balance = starting_balance
    day_start_balance = starting_balance
    daily_loss_pct = 0.0
    daily_losing_trades = 0
    active_day: dt.date | None = None
    events: list[ProfessionalFuturesEvent] = []

    for close_time, symbol, index in scheduled:
        event_day = dt.datetime.fromtimestamp(close_time / 1000, tz=dt.timezone.utc).date()
        if event_day != active_day:
            active_day = event_day
            day_start_balance = balance
            daily_loss_pct = 0.0
            daily_losing_trades = 0

        frames = market_data[symbol]
        close_times = close_times_by_symbol[symbol]
        windows = {
            timeframe: _window(frames[timeframe], close_times[timeframe], close_time)
            for timeframe in ("15m", "1h", "4h")
        }
        if any(not candles for candles in windows.values()):
            continue

        analysis = analyze_symbol(symbol, windows)
        previous_15m = frames["15m"][index - 1] if index > 0 else None
        flow_stats = {symbol: _market_stat(symbol, windows["15m"][-1], previous_15m)}
        confirm_stats = {symbol: _market_stat(symbol, windows["1h"][-1], windows["1h"][-2])}
        market_regime = btc_regime_at(close_time) if btc_regime_at else None
        futures_flow = _analysis_futures_flow(
            symbol,
            analysis,
            flow_histories.get(symbol),
            close_time,
        )
        signal = _futures_signal(
            analysis,
            symbol != "BTCUSDT" and btc_is_bearish(close_time),
            None,
            flow_stats,
            confirm_stats,
            None,
            None,
            None,
            market_regime,
            daily_loss_pct,
            daily_losing_trades,
            futures_flow,
        )
        if signal.entry is None or signal.target is None or signal.stop is None:
            continue

        side = signal.side
        if side not in {"LONG", "SHORT"}:
            if signal.risk_plan and signal.risk_plan.status in {RiskStatus.SKIP, RiskStatus.NO_TRADE}:
                events.append(
                    _skip_event(
                        cycle.name,
                        symbol,
                        close_time,
                        signal,
                        signal.risk_plan,
                        market_regime,
                        balance,
                        daily_loss_pct,
                        daily_losing_trades,
                        futures_flow,
                    )
                )
            continue

        risk_plan = _professional_risk_plan(
            signal,
            side,
            market_regime,
            balance,
            daily_loss_pct,
            daily_losing_trades,
        )
        if risk_plan.status in {RiskStatus.SKIP, RiskStatus.NO_TRADE}:
            events.append(
                _skip_event(
                    cycle.name,
                    symbol,
                    close_time,
                    signal,
                    risk_plan,
                    market_regime,
                    balance,
                    daily_loss_pct,
                    daily_losing_trades,
                    futures_flow,
                )
            )
            continue

        future = _future(frames["15m"], close_times["15m"], close_time)
        execution = _professional_trade_execution(
            future,
            side,
            float(signal.entry),
            float(signal.target),
            float(signal.stop),
            signal.needs_trigger,
            risk_plan,
            fee_rate_pct,
            slippage_pct,
        )
        if not execution:
            continue

        balance_before = balance
        daily_loss_before = daily_loss_pct
        daily_losing_before = daily_losing_trades
        net_pnl = float(execution["net_pnl"])
        balance = max(balance + net_pnl, 0.0)
        return_pct = (net_pnl / balance_before) * 100 if balance_before else 0.0
        if net_pnl < 0:
            daily_losing_trades += 1
            daily_loss_pct += (abs(net_pnl) / day_start_balance) * 100 if day_start_balance else 0.0

        events.append(
            ProfessionalFuturesEvent(
                period=cycle.name,
                symbol=symbol,
                opened_at=_iso_from_ms(int(execution["opened_at_ms"])),
                closed_at=_iso_from_ms(int(execution["closed_at_ms"])),
                side=side,
                bias=signal.bias,
                market_regime=market_regime.regime.value if market_regime else "UNKNOWN",
                confidence=signal.confidence,
                risk_status=risk_plan.status.value,
                risk_pct=risk_plan.risk_pct,
                risk_amount=risk_plan.risk_amount,
                rr=risk_plan.rr,
                entry=float(signal.entry),
                entry_fill=float(execution["entry_fill"]),
                target=float(signal.target),
                stop=float(signal.stop),
                exit_price=float(execution["exit_price"]),
                exit_reason=str(execution["exit_reason"]),
                quantity=float(execution["quantity"]),
                notional_value=float(execution["notional_value"]),
                fees=float(execution["fees"]),
                gross_pnl=float(execution["gross_pnl"]),
                net_pnl=net_pnl,
                return_pct=return_pct,
                balance_before=balance_before,
                balance_after=balance,
                daily_loss_pct_before=daily_loss_before,
                daily_losing_trades_before=daily_losing_before,
                **_flow_event_fields(futures_flow),
            )
        )

        if balance <= 0:
            break

    return _professional_summary(events, starting_balance), events


def _best_regime(events: list[ProfessionalFuturesEvent]) -> dict[str, object] | None:
    trades = [event for event in events if event.quantity > 0]
    if not trades:
        return None
    grouped: dict[str, list[ProfessionalFuturesEvent]] = {}
    for event in trades:
        grouped.setdefault(event.market_regime or "UNKNOWN", []).append(event)

    regime, regime_events = max(
        grouped.items(),
        key=lambda item: sum(event.net_pnl for event in item[1]),
    )
    wins = sum(1 for event in regime_events if event.net_pnl > 0)
    return {
        "regime": regime,
        "trades": len(regime_events),
        "win_rate": round((wins / len(regime_events)) * 100, 2) if regime_events else 0.0,
        "net_pnl": round(sum(event.net_pnl for event in regime_events), 4),
    }


def _professional_summary(
    events: list[ProfessionalFuturesEvent],
    starting_balance: float,
) -> ProfessionalSummary:
    trades = [event for event in events if event.quantity > 0]
    wins = [event for event in trades if event.net_pnl > 0]
    losses = [event for event in trades if event.net_pnl < 0]
    gross_profit = sum(event.net_pnl for event in wins)
    gross_loss = abs(sum(event.net_pnl for event in losses))
    profit_factor = (gross_profit / gross_loss) if gross_loss else (None if not gross_profit else None)

    peak = starting_balance
    max_drawdown_pct = 0.0
    for event in trades:
        peak = max(peak, event.balance_after)
        if peak > 0:
            drawdown = ((peak - event.balance_after) / peak) * 100
            max_drawdown_pct = max(max_drawdown_pct, drawdown)

    biggest_losing_streak = 0
    current_streak = 0
    for event in trades:
        if event.net_pnl < 0:
            current_streak += 1
            biggest_losing_streak = max(biggest_losing_streak, current_streak)
        elif event.net_pnl > 0:
            current_streak = 0

    final_balance = trades[-1].balance_after if trades else starting_balance
    return ProfessionalSummary(
        total_trades=len(trades),
        wins=len(wins),
        losses=len(losses),
        win_rate=round((len(wins) / len(trades)) * 100, 2) if trades else 0.0,
        profit_factor=round(profit_factor, 4) if profit_factor is not None else None,
        max_drawdown_pct=round(max_drawdown_pct, 2),
        biggest_losing_streak=biggest_losing_streak,
        average_win=round(gross_profit / len(wins), 4) if wins else 0.0,
        average_loss=round(gross_loss / len(losses), 4) if losses else 0.0,
        final_balance=round(final_balance, 4),
        net_profit=round(final_balance - starting_balance, 4),
        total_fees=round(sum(event.fees for event in trades), 4),
        no_trade=sum(1 for event in events if event.exit_reason == RiskStatus.NO_TRADE.value),
        risk_skips=sum(1 for event in events if event.exit_reason == RiskStatus.SKIP.value),
        best_performing_market_regime=_best_regime(events),
    )


def _aggregate_professional_summary(
    period_results: dict[str, dict[str, object]],
    starting_balance: float,
) -> dict[str, object]:
    all_events: list[ProfessionalFuturesEvent] = []
    summaries: list[ProfessionalSummary] = []
    for payload in period_results.values():
        summaries.append(payload["summary_obj"])
        all_events.extend(payload["events_obj"])

    trades = [event for event in all_events if event.quantity > 0]
    wins = [event for event in trades if event.net_pnl > 0]
    losses = [event for event in trades if event.net_pnl < 0]
    gross_profit = sum(event.net_pnl for event in wins)
    gross_loss = abs(sum(event.net_pnl for event in losses))
    final_balance = sum(summary.final_balance for summary in summaries)
    initial_balance = starting_balance * len(summaries)
    profit_factor = (gross_profit / gross_loss) if gross_loss else None
    return {
        "total_trades": len(trades),
        "win_rate": round((len(wins) / len(trades)) * 100, 2) if trades else 0.0,
        "profit_factor": round(profit_factor, 4) if profit_factor is not None else None,
        "max_drawdown_pct": max((summary.max_drawdown_pct for summary in summaries), default=0.0),
        "biggest_losing_streak": max((summary.biggest_losing_streak for summary in summaries), default=0),
        "average_win": round(gross_profit / len(wins), 4) if wins else 0.0,
        "average_loss": round(gross_loss / len(losses), 4) if losses else 0.0,
        "final_balance": round(final_balance, 4),
        "net_profit": round(final_balance - initial_balance, 4),
        "best_performing_market_regime": _best_regime(all_events),
    }


def run_professional_futures_backtest(
    symbols: tuple[str, ...],
    sample_every: int,
    starting_balance: float = DEFAULT_STARTING_BALANCE,
    fee_rate_pct: float = DEFAULT_FEE_RATE_PCT,
    slippage_pct: float = DEFAULT_SLIPPAGE_PCT,
    cycles: tuple[MarketCycle, ...] | None = None,
) -> dict[str, object]:
    cycles = cycles or _default_market_cycles()
    period_results: dict[str, dict[str, object]] = {}

    for cycle in cycles:
        summary, events = _run_professional_cycle(
            cycle,
            symbols,
            max(sample_every, 1),
            starting_balance,
            fee_rate_pct,
            slippage_pct,
        )
        period_results[cycle.name] = {
            "start": cycle.start.isoformat(),
            "end": cycle.end.isoformat(),
            "summary_obj": summary,
            "events_obj": events,
        }

    public_periods = {
        name: {
            "start": payload["start"],
            "end": payload["end"],
            "summary": asdict(payload["summary_obj"]),
            "events": [asdict(event) for event in payload["events_obj"][-1000:]],
        }
        for name, payload in period_results.items()
    }
    return {
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "mode": "professional_futures",
        "data_source": "Binance USD-M Futures /fapi/v1/klines + futures flow endpoints",
        "symbols_tested": list(symbols),
        "starting_balance": starting_balance,
        "fee_rate_pct_per_side": fee_rate_pct,
        "slippage_pct": slippage_pct,
        "sample_every_15m": max(sample_every, 1),
        "aggregate": _aggregate_professional_summary(period_results, starting_balance),
        "periods": public_periods,
    }


def _score(metrics: BacktestMetrics) -> float:
    trade_penalty = max(0, 5 - metrics.trades) * 0.5
    loss_penalty = metrics.losses * 0.7
    return metrics.total_return_pct + metrics.win_rate * 0.08 - abs(metrics.max_loss_pct) * 0.5 - trade_penalty - loss_penalty


def _unsafe_metrics(metrics: BacktestMetrics) -> bool:
    closed = metrics.wins + metrics.losses
    return closed >= 5 and metrics.losses > metrics.wins and metrics.win_rate < 40 and metrics.total_return_pct < 0


def _optimize(events: list[SignalEvent]) -> tuple[dict[str, float], BacktestMetrics]:
    default_settings = {"min_entry_confidence": 65, "min_entry_rr": 2.0}
    default_metrics = _metrics(events, 65, 2.0)
    best_settings = dict(default_settings)
    best_metrics = default_metrics
    best_score = _score(best_metrics)
    candidates: list[tuple[float, dict[str, float], BacktestMetrics]] = [
        (best_score, dict(best_settings), best_metrics)
    ]

    for min_confidence in (55, 60, 65, 70, 75):
        for min_rr in (1.5, 2.0, 2.5, 3.0):
            metrics = _metrics(events, min_confidence, min_rr)
            score = _score(metrics)
            settings = {"min_entry_confidence": min_confidence, "min_entry_rr": min_rr}
            candidates.append((score, settings, metrics))
            if score > best_score:
                best_score = score
                best_settings = settings
                best_metrics = metrics

    if _unsafe_metrics(best_metrics):
        safe_candidates = [
            (score, settings, metrics)
            for score, settings, metrics in candidates
            if not _unsafe_metrics(metrics)
        ]
        if safe_candidates:
            best_score, best_settings, best_metrics = max(safe_candidates, key=lambda item: item[0])
        else:
            best_settings = {"min_entry_confidence": 75, "min_entry_rr": 3.0}
            best_metrics = _metrics(events, 75, 3.0)

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
    regime_warmup_days = 320
    start = now - dt.timedelta(days=days)
    fetch_start = start - dt.timedelta(days=warmup_days)
    regime_fetch_start = start - dt.timedelta(days=regime_warmup_days)
    start_ms = _utc_ms(start)
    fetch_start_ms = _utc_ms(fetch_start)
    regime_fetch_start_ms = _utc_ms(regime_fetch_start)
    end_ms = _utc_ms(now)

    fetch_symbols = tuple(dict.fromkeys(("BTCUSDT", *symbols)))
    market_data: dict[str, dict[str, list[Candle]]] = {}
    for symbol in fetch_symbols:
        market_data[symbol] = {
            timeframe: _fetch_klines_paginated(symbol, timeframe, fetch_start_ms, end_ms)
            for timeframe in ("15m", "1h", "4h")
        }
        if symbol == "BTCUSDT":
            market_data[symbol]["1d"] = _fetch_klines_paginated(
                symbol,
                "1d",
                regime_fetch_start_ms,
                end_ms,
            )

    btc_is_bearish = _btc_cache_builder(market_data["BTCUSDT"])
    btc_regime_at = _btc_regime_cache_builder(market_data["BTCUSDT"])
    flow_histories = _fetch_futures_flow_histories(symbols, start_ms, end_ms)
    events: list[SignalEvent] = []
    for symbol in symbols:
        events.extend(_collect_symbol_events(symbol, market_data[symbol], btc_is_bearish, start_ms, sample_every))
    futures_events = _collect_futures_events_chronological(
        symbols,
        market_data,
        btc_is_bearish,
        btc_regime_at,
        start_ms,
        sample_every,
        flow_histories,
    )

    optimized, metrics = _optimize(events)
    futures_metrics = _futures_metrics(futures_events)
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
        "futures_metrics": asdict(futures_metrics),
        "symbols": symbol_weights,
        "events": [asdict(event) for event in events[-500:]],
        "futures_events": [asdict(event) for event in futures_events[-500:]],
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Professional Binance futures backtester")
    parser.add_argument("--legacy-spot", action="store_true", help="Run the old spot optimizer backtest")
    parser.add_argument("--days", type=int, default=30, help="Legacy spot backtest period in days")
    parser.add_argument("--sample-every", type=int, default=4, help="Evaluate every N 15m candles")
    parser.add_argument("--all-symbols", action="store_true", help="Backtest every configured symbol")
    parser.add_argument("--symbols", nargs="*", default=None, help="Symbols to backtest, e.g. BTCUSDT ETHUSDT SOLUSDT")
    parser.add_argument("--output", default="backtest_results.json", help="Where to write full backtest output")
    parser.add_argument("--weights-output", default="signal_weights.json", help="Where to write optimized bot weights")
    parser.add_argument("--starting-balance", type=float, default=DEFAULT_STARTING_BALANCE, help="Starting futures balance in USDT")
    parser.add_argument("--fee-rate-pct", type=float, default=DEFAULT_FEE_RATE_PCT, help="Trading fee percent per side")
    parser.add_argument("--slippage-pct", type=float, default=DEFAULT_SLIPPAGE_PCT, help="Adverse slippage percent per fill")
    parser.add_argument(
        "--periods",
        nargs="*",
        default=None,
        help="Optional periods: 2021_bull_market 2022_bear_market 2024_bull_market last_180_days",
    )
    return parser.parse_args()


def _cycles_from_args(period_names: list[str] | None) -> tuple[MarketCycle, ...]:
    cycles = _default_market_cycles()
    if not period_names:
        return cycles
    wanted = set(period_names)
    return tuple(cycle for cycle in cycles if cycle.name in wanted)


def main() -> int:
    args = parse_args()
    if args.all_symbols:
        symbols = ALL_SYMBOLS
    elif args.symbols:
        symbols = tuple(symbol.upper() for symbol in args.symbols)
    else:
        symbols = DEFAULT_SYMBOLS

    if args.legacy_spot:
        result = run_backtest(symbols=symbols, days=args.days, sample_every=max(args.sample_every, 1))
        Path(args.output).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

        weights = {key: result[key] for key in ("generated_at", "days", "sample_every_15m", "symbols_tested", "optimized", "metrics", "symbols")}
        Path(args.weights_output).write_text(json.dumps(weights, ensure_ascii=False, indent=2), encoding="utf-8")

        metrics = result["metrics"]
        futures_metrics = result["futures_metrics"]
        optimized = result["optimized"]
        print("LEGACY BACKTEST TAMAM")
        print(f"Sembol: {', '.join(symbols)}")
        print(f"En iyi güven: {optimized['min_entry_confidence']} | R/R: {optimized['min_entry_rr']}")
        print(
            "Sonuç: "
            f"trade {metrics['trades']} | win {metrics['wins']} | loss {metrics['losses']} | "
            f"başarı %{metrics['win_rate']:.1f} | toplam {metrics['total_return_pct']:+.2f}% | "
            f"kaçan {metrics['missed']} | korunan {metrics['protected']}"
        )
        print(
            "Futures: "
            f"trade {futures_metrics['trades']} | win {futures_metrics['wins']} | loss {futures_metrics['losses']} | "
            f"başarı %{futures_metrics['win_rate']:.1f} | toplam {futures_metrics['total_return_pct']:+.2f}% | "
            f"no_trade {futures_metrics['protected']}"
        )
        print(f"Yazıldı: {args.output}, {args.weights_output}")
        return 0

    result = run_professional_futures_backtest(
        symbols=symbols,
        sample_every=max(args.sample_every, 1),
        starting_balance=args.starting_balance,
        fee_rate_pct=args.fee_rate_pct,
        slippage_pct=args.slippage_pct,
        cycles=_cycles_from_args(args.periods),
    )
    Path(args.output).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    aggregate = result["aggregate"]
    print("PRO FUTURES BACKTEST TAMAM")
    print(f"Sembol: {', '.join(symbols)}")
    print(f"Başlangıç: {args.starting_balance:.2f} USDT | Fee {args.fee_rate_pct:.3f}% | Slippage {args.slippage_pct:.3f}%")
    print(
        "Toplam: "
        f"trade {aggregate['total_trades']} | win rate %{aggregate['win_rate']:.1f} | "
        f"PF {aggregate['profit_factor']} | DD %{aggregate['max_drawdown_pct']:.1f} | "
        f"final {aggregate['final_balance']:.2f} USDT"
    )
    print(f"En iyi rejim: {aggregate['best_performing_market_regime']}")
    print(f"Yazıldı: {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
