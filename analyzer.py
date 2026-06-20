"""Analysis logic for symbols and timeframes."""

from __future__ import annotations

import datetime as dt
import os
from dataclasses import dataclass
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from data_fetcher import Candle
from data_fetcher import MarketStat, OrderBookPressure
from exchange_fetcher import ExchangeConsensus
from futures_flow import FuturesFlowSnapshot
from futures_tracker import FuturesSignalCandidate, daily_futures_risk_state, track_futures_signals
from indicators import bollinger_bands, ema, macd, momentum_pct, previous_ema_pair, rsi, sma
from macro_fetcher import MarketContext
from market_regime import (
    MarketRegime,
    MarketRegimeResult,
    RegimeFrame,
    classify_market_regime,
    is_near_middle_of_range,
)
from onchain_fetcher import OnChainFlow, StablecoinReserve
from risk_engine import RiskPlan, RiskStatus, calculate_futures_risk
from signal_weights import load_signal_weights
from signal_tracker import (
    SignalCandidate,
    SignalPerformanceProfile,
    load_signal_performance_profile,
    track_signals,
)


MIN_ENTRY_CONFIDENCE = int(os.getenv("MIN_ENTRY_CONFIDENCE", "65"))
MIN_ENTRY_RR = float(os.getenv("MIN_ENTRY_RR", "2.0"))
QUALITY_LOW_24H_VOLUME = float(os.getenv("QUALITY_LOW_24H_VOLUME", "30000000"))
QUALITY_LOW_FLOW_VOLUME = float(os.getenv("QUALITY_LOW_FLOW_VOLUME", "75000"))
REPORT_TIMEZONE = os.getenv("REPORT_TIMEZONE", "Europe/Brussels")
REPORT_PROFILE_VERSION = os.getenv("REPORT_PROFILE_VERSION", "BTC/ETH v2")
FUTURES_MODE_ENABLED = os.getenv("FUTURES_MODE_ENABLED", "true").lower() not in {"0", "false", "no", "off"}
FUTURES_MIN_CONFIDENCE = int(os.getenv("FUTURES_MIN_CONFIDENCE", "72"))
FUTURES_MIN_EDGE = int(os.getenv("FUTURES_MIN_EDGE", "8"))
FUTURES_MIN_RR = float(os.getenv("FUTURES_MIN_RR", "1.8"))
FUTURES_ACCOUNT_BALANCE = float(os.getenv("FUTURES_ACCOUNT_BALANCE", "500"))
FUTURES_MAX_RISK_PCT = float(os.getenv("FUTURES_MAX_RISK_PCT", "2"))
FUTURES_MAX_LEVERAGE = int(os.getenv("FUTURES_MAX_LEVERAGE", "5"))
FUTURES_MAX_STOP_LOSS_PCT = float(os.getenv("FUTURES_MAX_STOP_LOSS_PCT", "5"))
FUTURES_MAX_DAILY_LOSS_PCT = float(os.getenv("FUTURES_MAX_DAILY_LOSS_PCT", "5"))
FUTURES_MAX_DAILY_LOSING_TRADES = int(os.getenv("FUTURES_MAX_DAILY_LOSING_TRADES", "3"))
FUTURES_TODAY_LOSS_PCT = float(os.getenv("FUTURES_TODAY_LOSS_PCT", "0"))
FUTURES_TODAY_LOSING_TRADES = int(os.getenv("FUTURES_TODAY_LOSING_TRADES", "0"))


@dataclass(frozen=True)
class TimeframeAnalysis:
    timeframe: str
    trend: str
    trend_score: int
    structure: str
    close: float
    change_pct: float
    rsi: float
    ema7: float
    ema20: float
    ema50: float
    ema50_slope_pct: float
    ema200: float
    macd_histogram: float
    momentum_pct: float
    bollinger_lower: float
    bollinger_middle: float
    bollinger_upper: float
    volume_change_pct: float
    taker_buy_ratio: float
    taker_delta_pct: float
    fake_rise_risk: bool
    distribution_risk: bool
    support: float
    resistance: float
    recent_high: float
    recent_low: float
    candle_pattern: str | None
    warnings: list[str]


@dataclass(frozen=True)
class SymbolAnalysis:
    symbol: str
    timeframes: list[TimeframeAnalysis]


@dataclass(frozen=True)
class FuturesSignal:
    side: str
    bias: str
    confidence: int
    opposite_confidence: int
    reason: str
    entry: float | None
    target: float | None
    stop: float | None
    rr: float | None
    needs_trigger: bool = False
    market_regime: MarketRegimeResult | None = None
    risk_plan: RiskPlan | None = None
    futures_flow: FuturesFlowSnapshot | None = None


def _trend_from_score(score: int) -> str:
    if score >= 5:
        return "bullish"
    if score <= -5:
        return "bearish"
    return "neutral"


def _report_time_line() -> str:
    try:
        timezone = ZoneInfo(REPORT_TIMEZONE)
    except ZoneInfoNotFoundError:
        timezone = ZoneInfo("Europe/Brussels")
    now = dt.datetime.now(timezone)
    return f"Tarih: {now:%d.%m.%Y} | Saat: {now:%H:%M} {now:%Z}"


def _swing_levels(candles: list[Candle], lookback: int = 30) -> tuple[float, float]:
    completed = candles[:-1] if len(candles) > 1 else candles
    recent = completed[-lookback:] or candles[-lookback:]
    support = min(c.low for c in recent)
    resistance = max(c.high for c in recent)
    return support, resistance


def _pivot_values(candles: list[Candle], field: str, lookback: int = 80) -> list[float]:
    if len(candles) < 10:
        return []

    left = 2
    right = 2
    start = max(left, len(candles) - lookback)
    end = len(candles) - right
    pivots: list[float] = []

    for index in range(start, end):
        current = getattr(candles[index], field)
        window = [getattr(candle, field) for candle in candles[index - left:index + right + 1]]
        if field == "high" and current == max(window):
            pivots.append(current)
        elif field == "low" and current == min(window):
            pivots.append(current)
    return pivots[-2:]


def _market_structure(candles: list[Candle], lookback: int = 80) -> str:
    completed = candles[:-1] if len(candles) > 1 else candles
    highs = _pivot_values(completed, "high", lookback)
    lows = _pivot_values(completed, "low", lookback)

    if len(highs) < 2 or len(lows) < 2:
        recent = completed[-lookback:]
        if len(recent) < 20:
            return "range"
        midpoint = len(recent) // 2
        highs = [max(c.high for c in recent[:midpoint]), max(c.high for c in recent[midpoint:])]
        lows = [min(c.low for c in recent[:midpoint]), min(c.low for c in recent[midpoint:])]

    higher_high = highs[-1] > highs[-2]
    higher_low = lows[-1] > lows[-2]
    lower_high = highs[-1] < highs[-2]
    lower_low = lows[-1] < lows[-2]

    if higher_high and higher_low:
        return "up"
    if lower_high and lower_low:
        return "down"
    return "range"


def _trend_score(
    close: float,
    previous_close: float,
    ema20_now: float,
    ema50_now: float,
    ema200_now: float,
    ema20_prev: float,
    ema50_prev: float,
    macd_hist: float,
    macd_hist_prev: float,
    momentum: float,
    volume_change_pct: float,
    taker_delta_pct: float,
    support: float,
    resistance: float,
    structure: str,
) -> int:
    score = 0

    score += 1 if close > ema20_now else -1
    score += 1 if close > ema50_now else -1
    score += 1 if ema20_now > ema50_now else -1
    score += 1 if ema50_now > ema200_now else -1

    if ema20_now > ema20_prev and ema50_now >= ema50_prev:
        score += 1
    elif ema20_now < ema20_prev and ema50_now <= ema50_prev:
        score -= 1

    if macd_hist > 0:
        score += 1
    elif macd_hist < 0:
        score -= 1

    if macd_hist > macd_hist_prev:
        score += 1
    elif macd_hist < macd_hist_prev:
        score -= 1

    if momentum >= 0.25:
        score += 1
    elif momentum <= -0.25:
        score -= 1

    if structure == "up":
        score += 2
    elif structure == "down":
        score -= 2

    if resistance and close > resistance:
        score += 2
    elif support and close < support:
        score -= 2

    if volume_change_pct >= 50:
        score += 1 if close >= previous_close else -1

    if taker_delta_pct >= 10:
        score += 1
    elif taker_delta_pct <= -10:
        score -= 1

    if close > previous_close and taker_delta_pct <= -10:
        score -= 1

    return score


def _quote_volume(candle: Candle) -> float:
    return candle.quote_volume or candle.close * candle.volume


def _taker_buy_ratio(candles: list[Candle], lookback: int = 5) -> float:
    recent = candles[-lookback:]
    total_quote = sum(_quote_volume(candle) for candle in recent)
    buy_quote = sum(candle.taker_buy_quote_volume for candle in recent)
    if total_quote <= 0:
        return 50.0
    return (buy_quote / total_quote) * 100


def _upper_wick_pct(candle: Candle) -> float:
    full_range = candle.high - candle.low
    if full_range <= 0:
        return 0.0
    upper_wick = candle.high - max(candle.open, candle.close)
    return (upper_wick / full_range) * 100


def _fake_rise_risk(
    last: Candle,
    previous: Candle,
    resistance: float,
    volume_change_pct: float,
    taker_buy_ratio: float,
) -> bool:
    price_positive = last.close > previous.close
    failed_breakout = bool(resistance and last.high > resistance and last.close < resistance)
    weak_buying = taker_buy_ratio < 49
    high_volume = volume_change_pct >= 50
    upper_wick_heavy = _upper_wick_pct(last) >= 40
    green_candle = last.close > last.open

    return (
        failed_breakout and upper_wick_heavy
        or price_positive and weak_buying and high_volume
        or green_candle and upper_wick_heavy and weak_buying
    )


def _distribution_risk(
    last: Candle,
    previous: Candle,
    volume_change_pct: float,
    taker_buy_ratio: float,
    fake_rise_risk: bool,
) -> bool:
    price_positive = last.close >= previous.close
    seller_control = taker_buy_ratio <= 45
    return fake_rise_risk or (price_positive and seller_control and volume_change_pct >= 50)


def _candle_pattern(candles: list[Candle]) -> str | None:
    if len(candles) < 2:
        return None
    previous = candles[-2]
    last = candles[-1]
    body = abs(last.close - last.open)
    full_range = last.high - last.low
    if full_range <= 0:
        return None

    upper_shadow = last.high - max(last.open, last.close)
    lower_shadow = min(last.open, last.close) - last.low
    body_ratio = body / full_range

    if body_ratio <= 0.10:
        return "Doji"
    if lower_shadow >= body * 2 and upper_shadow <= body * 0.6 and last.close >= last.open:
        return "Hammer"

    previous_bearish = previous.close < previous.open
    previous_bullish = previous.close > previous.open
    current_bullish = last.close > last.open
    current_bearish = last.close < last.open
    bullish_engulfing = (
        previous_bearish
        and current_bullish
        and last.open <= previous.close
        and last.close >= previous.open
    )
    bearish_engulfing = (
        previous_bullish
        and current_bearish
        and last.open >= previous.close
        and last.close <= previous.open
    )
    if bullish_engulfing:
        return "Bullish Engulfing"
    if bearish_engulfing:
        return "Bearish Engulfing"
    return None


def _warnings(
    last_rsi: float,
    close: float,
    volume_change_pct: float,
    momentum: float,
    bollinger_lower: float,
    bollinger_upper: float,
    candle_pattern: str | None,
    taker_delta_pct: float,
    fake_rise_risk: bool,
    distribution_risk: bool,
    ema20_now: float,
    ema50_now: float,
    ema20_prev: float,
    ema50_prev: float,
) -> list[str]:
    warnings: list[str] = []
    if last_rsi >= 70:
        warnings.append(f"RSI asiri yuksek ({last_rsi:.0f})")
    elif last_rsi <= 30:
        warnings.append(f"RSI asiri dusuk ({last_rsi:.0f})")

    if volume_change_pct >= 80:
        warnings.append(f"Ani hacim artisi ({volume_change_pct:+.0f}%)")

    if close <= bollinger_lower:
        warnings.append("Bollinger alt banda temas")
    elif close >= bollinger_upper:
        warnings.append("Bollinger ust banda temas")

    if momentum <= -2.0:
        warnings.append(f"Momentum zayif ({momentum:+.1f}%)")
    elif momentum >= 2.0:
        warnings.append(f"Momentum guclu ({momentum:+.1f}%)")

    if candle_pattern:
        warnings.append(f"Mum: {candle_pattern}")

    if taker_delta_pct <= -15 and volume_change_pct >= 50:
        warnings.append("Büyük satış izi")
    elif taker_delta_pct >= 15 and volume_change_pct >= 50:
        warnings.append("Büyük alım izi")

    if fake_rise_risk:
        warnings.append("Fake yükseliş riski")
    if distribution_risk:
        warnings.append("Dağıtım riski")

    crossed_up = ema20_prev <= ema50_prev and ema20_now > ema50_now
    crossed_down = ema20_prev >= ema50_prev and ema20_now < ema50_now
    if crossed_up:
        warnings.append("EMA20, EMA50 uzerine kesti")
    elif crossed_down:
        warnings.append("EMA20, EMA50 altina kesti")

    return warnings


def analyze_timeframe(timeframe: str, candles: list[Candle]) -> TimeframeAnalysis:
    if len(candles) < 210:
        raise ValueError(f"{timeframe} icin en az 210 mum gerekli")

    closes = [c.close for c in candles]
    volumes = [c.volume for c in candles]
    last = candles[-1]
    previous = candles[-2]
    recent_window = candles[-4:] if timeframe == "15m" else candles[-2:]
    recent_high = max(c.high for c in recent_window)
    recent_low = min(c.low for c in recent_window)

    last_rsi = rsi(closes)
    ema7_now = ema(closes, 7)
    ema20_now = ema(closes, 20)
    ema50_now = ema(closes, 50)
    ema200_now = ema(closes, 200)
    ema20_prev, ema50_prev = previous_ema_pair(closes, 20, 50)
    ema50_slope_pct = ((ema50_now - ema50_prev) / ema50_prev) * 100 if ema50_prev else 0.0
    _, _, macd_hist = macd(closes)
    _, _, macd_hist_prev = macd(closes[:-1])
    lower_band, middle_band, upper_band = bollinger_bands(closes)
    momentum = momentum_pct(closes)
    avg_volume = sma(volumes[:-1], 20)
    volume_change_pct = ((last.volume - avg_volume) / avg_volume) * 100 if avg_volume else 0.0
    price_change_pct = ((last.close - previous.close) / previous.close) * 100
    support, resistance = _swing_levels(candles)
    taker_buy_ratio = _taker_buy_ratio(candles)
    taker_delta_pct = (taker_buy_ratio - 50) * 2
    fake_rise_risk = _fake_rise_risk(
        last,
        previous,
        resistance,
        volume_change_pct,
        taker_buy_ratio,
    )
    distribution_risk = _distribution_risk(
        last,
        previous,
        volume_change_pct,
        taker_buy_ratio,
        fake_rise_risk,
    )
    structure = _market_structure(candles)
    trend_score = _trend_score(
        last.close,
        previous.close,
        ema20_now,
        ema50_now,
        ema200_now,
        ema20_prev,
        ema50_prev,
        macd_hist,
        macd_hist_prev,
        momentum,
        volume_change_pct,
        taker_delta_pct,
        support,
        resistance,
        structure,
    )
    candle_pattern = _candle_pattern(candles)

    return TimeframeAnalysis(
        timeframe=timeframe,
        trend=_trend_from_score(trend_score),
        trend_score=trend_score,
        structure=structure,
        close=last.close,
        change_pct=price_change_pct,
        rsi=last_rsi,
        ema7=ema7_now,
        ema20=ema20_now,
        ema50=ema50_now,
        ema50_slope_pct=ema50_slope_pct,
        ema200=ema200_now,
        macd_histogram=macd_hist,
        momentum_pct=momentum,
        bollinger_lower=lower_band,
        bollinger_middle=middle_band,
        bollinger_upper=upper_band,
        volume_change_pct=volume_change_pct,
        taker_buy_ratio=taker_buy_ratio,
        taker_delta_pct=taker_delta_pct,
        fake_rise_risk=fake_rise_risk,
        distribution_risk=distribution_risk,
        support=support,
        resistance=resistance,
        recent_high=recent_high,
        recent_low=recent_low,
        candle_pattern=candle_pattern,
        warnings=_warnings(
            last_rsi,
            last.close,
            volume_change_pct,
            momentum,
            lower_band,
            upper_band,
            candle_pattern,
            taker_delta_pct,
            fake_rise_risk,
            distribution_risk,
            ema20_now,
            ema50_now,
            ema20_prev,
            ema50_prev,
        ),
    )


def analyze_symbol(symbol: str, candles_by_timeframe: dict[str, list[Candle]]) -> SymbolAnalysis:
    analyses = [
        analyze_timeframe(timeframe, candles)
        for timeframe, candles in candles_by_timeframe.items()
    ]
    return SymbolAnalysis(symbol=symbol, timeframes=analyses)


def _trend_label(item: TimeframeAnalysis) -> str:
    if item.trend == "bullish":
        if item.trend_score >= 8:
            return "GÜÇLÜ YÜKSELİŞ 🟢"
        return "YÜKSELİŞ 🟢"
    if item.trend == "bearish":
        if item.trend_score <= -8:
            return "GÜÇLÜ DÜŞÜŞ 🔴"
        return "ZAYIF 🔴"
    if item.trend_score >= 3:
        return "TOPARLANMA 🟢"
    if item.trend_score <= -3:
        return "ZAYIFLAMA 🔴"
    return "NÖTR 🟡"


def _timeframe_label(timeframe: str) -> str:
    return timeframe.upper()


def _fmt_price(value: float) -> str:
    if value >= 1000:
        return f"{value:.0f}"
    if value >= 100:
        return f"{value:.1f}".rstrip("0").rstrip(".")
    if value >= 10:
        return f"{value:.2f}".rstrip("0").rstrip(".")
    if value >= 1:
        return f"{value:.3f}".rstrip("0").rstrip(".")
    if value >= 0.1:
        return f"{value:.4f}".rstrip("0").rstrip(".")
    return f"{value:.6f}".rstrip("0").rstrip(".")


def _fmt_quantity(value: float) -> str:
    if value >= 100:
        return f"{value:.2f}".rstrip("0").rstrip(".")
    if value >= 1:
        return f"{value:.4f}".rstrip("0").rstrip(".")
    return f"{value:.6f}".rstrip("0").rstrip(".")


def _fmt_amount(value: float | None, suffix: str = "") -> str:
    if value is None:
        return "?"
    absolute = abs(value)
    sign = "+" if value > 0 else "-" if value < 0 else ""
    if absolute >= 1_000_000_000:
        number = f"{absolute / 1_000_000_000:.1f}B"
    elif absolute >= 1_000_000:
        number = f"{absolute / 1_000_000:.1f}M"
    elif absolute >= 1_000:
        number = f"{absolute / 1_000:.1f}K"
    elif absolute >= 100:
        number = f"{absolute:.0f}"
    else:
        number = f"{absolute:.2f}".rstrip("0").rstrip(".")
    return f"{sign}{number}{suffix}"


def _fmt_abs_amount(value: float | None, suffix: str = "") -> str:
    if value is None:
        return "?"
    return _fmt_amount(abs(value), suffix).lstrip("+")


def _flow_pressure_usd(stat: MarketStat | None) -> float | None:
    if not stat:
        return None
    if stat.net_taker_quote_volume:
        return stat.net_taker_quote_volume
    return stat.quote_volume * (stat.price_change_pct / 100)


def _flow_strength(value: float | None) -> str:
    if value is None:
        return "belirsiz"
    absolute = abs(value)
    if absolute < 25_000:
        return "çok zayıf"
    if absolute < 100_000:
        return "zayıf"
    if absolute < 500_000:
        return "küçük"
    if absolute < 2_000_000:
        return "orta"
    return "güçlü"


def _show_learning_note(note: object, confidence: int) -> bool:
    if not isinstance(note, str):
        return False
    return confidence >= 45 or "Fırsat" in note or "fırsat" in note


def _flow_totals(stats: dict[str, MarketStat] | None) -> tuple[float, float, float] | None:
    if not stats:
        return None
    buy_total = sum(stat.taker_buy_quote_volume for stat in stats.values())
    sell_total = sum(stat.taker_sell_quote_volume for stat in stats.values())
    if buy_total <= 0 and sell_total <= 0:
        return None
    return buy_total, sell_total, buy_total - sell_total


def _flow_totals_suffix(stats: dict[str, MarketStat] | None) -> str:
    totals = _flow_totals(stats)
    if not totals:
        return ""
    buy_total, sell_total, _ = totals
    return f" | Alış {_fmt_abs_amount(buy_total, '$')} / Satış {_fmt_abs_amount(sell_total, '$')}"


def _sector_flow_amounts(
    stats: dict[str, MarketStat] | None,
    sectors: dict[str, str] | None,
) -> tuple[dict[str, float], dict[str, list[tuple[float, str]]]]:
    if not stats or not sectors:
        return {}, {}

    amounts: dict[str, float] = {}
    symbols: dict[str, list[tuple[float, str]]] = {}
    for symbol, stat in stats.items():
        sector = sectors.get(symbol)
        if not sector:
            continue
        amount = _flow_pressure_usd(stat)
        if amount is None:
            continue
        amounts[sector] = amounts.get(sector, 0.0) + amount
        symbols.setdefault(sector, []).append((amount, symbol))
    return amounts, symbols


def _top_flow_symbol(items: list[tuple[float, str]]) -> str | None:
    positives = [(amount, symbol) for amount, symbol in items if amount > 0]
    if not positives:
        return None
    amount, symbol = max(positives, key=lambda item: item[0])
    return f"{symbol.replace('USDT', '')} {_fmt_amount(amount, '$')}"


def _top_flow_names(items: list[tuple[float, str]], limit: int = 3) -> str:
    positives = [
        symbol.replace("USDT", "")
        for amount, symbol in sorted(items, reverse=True)
        if amount > 0
    ]
    return ", ".join(positives[:limit])


def _symbol_flow_line(
    symbol: str,
    flow_stats: dict[str, MarketStat] | None,
    confirm_stats: dict[str, MarketStat] | None,
) -> str | None:
    flow = flow_stats.get(symbol) if flow_stats else None
    confirm = confirm_stats.get(symbol) if confirm_stats else None
    if not flow and not confirm:
        return None

    if flow:
        amount = _flow_pressure_usd(flow)
        if flow.taker_buy_quote_volume > 0 or flow.taker_sell_quote_volume > 0:
            text = (
                f"Akış: Alış {_fmt_abs_amount(flow.taker_buy_quote_volume, '$')} | "
                f"Satış {_fmt_abs_amount(flow.taker_sell_quote_volume, '$')} | "
                f"Net {_fmt_amount(amount, '$')} {_flow_strength(amount)}"
            )
        else:
            text = f"Akış: Net {_fmt_amount(amount, '$')} {_flow_strength(amount)}"
        if confirm:
            text += f" | teyit {confirm.price_change_pct:+.1f}%"
        return text

    if confirm:
        amount = _flow_pressure_usd(confirm)
        return f"Teyit Akışı: {_fmt_amount(amount, '$')} {_flow_strength(amount)}"
    return None


def _cost_for(symbol: str, costs: dict[str, float] | None) -> float | None:
    if not costs:
        return None
    return costs.get(symbol.upper())


def _round_level(value: float) -> float:
    if value >= 1000:
        return round(value / 100) * 100
    if value >= 100:
        return round(value / 10) * 10
    return round(value)


def _alarm_text(item: TimeframeAnalysis, cost: float | None) -> str:
    alarms = list(item.warnings)
    if item.close >= item.resistance:
        alarms.insert(0, f"{_fmt_price(item.resistance)} kırıldı ✅")
    elif item.close <= item.support:
        alarms.insert(0, f"{_fmt_price(item.support)} destek kırıldı ⚠️")
    elif cost is not None:
        round_level = _round_level(item.close)
        if round_level and item.close < round_level:
            alarms.insert(0, f"{_fmt_price(round_level)} altına indi ⚠️")
    return "\n".join(alarms) if alarms else "Yok"


def _alarm_short(item: TimeframeAnalysis, cost: float | None) -> str | None:
    alarm = _alarm_text(item, cost)
    if alarm == "Yok":
        return None
    first_line = alarm.splitlines()[0]
    return first_line


def _decision(item: TimeframeAnalysis, cost: float | None, altcoin_blocked: bool = False) -> str:
    if altcoin_blocked:
        return "BTC ZAYIF - BEKLE"
    if item.close <= item.support:
        return "RISK VAR"
    if item.trend_score <= -5:
        return "BEKLE"
    near_support_pct = ((item.close - item.support) / item.support) * 100 if item.support else 999
    reversal_pattern = item.candle_pattern in {"Hammer", "Bullish Engulfing"}
    if near_support_pct <= 1.5 and item.rsi <= 50 and reversal_pattern:
        return "KADEMELİ ALIM"
    if near_support_pct <= 1.0 and item.rsi <= 45:
        return "ALIM BÖLGESİ"
    if cost is not None:
        pnl = ((item.close - cost) / cost) * 100
        if pnl >= 2.0:
            return "KISMİ SAT"
    if item.trend_score >= 6 and item.close >= item.ema20:
        return "TREND TAKİBİ"
    if item.trend_score >= 3 and item.close >= item.ema20 and item.rsi < 68:
        return "ERKEN TAKİP"
    if item.trend == "bullish" and item.close >= item.resistance:
        return "TAKIP ET"
    if item.trend_score <= -3:
        return "BEKLE"
    if cost is not None and item.close < cost:
        return "BEKLE"
    if item.trend == "neutral":
        return "İZLE"
    return "İZLE"


def _orderbook_state(order_book: OrderBookPressure | None) -> str | None:
    if not order_book:
        return None
    if order_book.imbalance_pct <= -20:
        return "OB satış duvarı"
    if order_book.imbalance_pct >= 20:
        return "OB alış duvarı"
    if (
        order_book.nearest_sell_wall_quote > 0
        and order_book.nearest_sell_wall_quote > order_book.nearest_buy_wall_quote * 1.5
    ):
        return "OB satış duvarı"
    if (
        order_book.nearest_buy_wall_quote > 0
        and order_book.nearest_buy_wall_quote > order_book.nearest_sell_wall_quote * 1.5
    ):
        return "OB alış duvarı"
    return None


def _onchain_sell_pressure(flow: OnChainFlow | None) -> bool:
    if not flow or flow.netflow is None:
        return False
    return flow.netflow > 0 and (flow.netflow_change_pct is None or flow.netflow_change_pct >= -20)


def _onchain_accumulation(flow: OnChainFlow | None) -> bool:
    if not flow or flow.netflow is None:
        return False
    return flow.netflow < 0


def _stablecoin_buy_power(stablecoin_reserve: StablecoinReserve | None) -> bool:
    if not stablecoin_reserve:
        return False
    change = stablecoin_reserve.reserve_usd_change_pct
    if change is None:
        change = stablecoin_reserve.reserve_change_pct
    return bool(change is not None and change >= 0.2)


def _stablecoin_line(stablecoin_reserve: StablecoinReserve | None) -> str | None:
    if not stablecoin_reserve:
        return None
    change = stablecoin_reserve.reserve_usd_change_pct
    value = stablecoin_reserve.reserve_usd
    if change is None:
        change = stablecoin_reserve.reserve_change_pct
        value = stablecoin_reserve.reserve
    if change is None:
        return None
    exchange_name = _exchange_label(stablecoin_reserve.exchange)
    if change >= 0.2:
        return f"Zincir: {exchange_name} stablecoin +{change:.1f}% | alım gücü artıyor 🟢"
    if change <= -0.2:
        return f"Zincir: {exchange_name} stablecoin {change:.1f}% | nakit çıkışı ⚠️"
    return f"Zincir: Stablecoin yatay | {_fmt_amount(value, '$')}"


def _exchange_label(exchange: str | None) -> str:
    if not exchange:
        return "Borsa"
    labels = {
        "all_exchange": "Tüm borsalar",
        "binance": "Binance",
    }
    return labels.get(exchange, exchange.replace("_", " ").title())


def _onchain_line(flow: OnChainFlow | None) -> str | None:
    if not flow or flow.netflow is None:
        return None
    amount = _fmt_amount(flow.netflow, f" {flow.asset}")
    exchange_name = _exchange_label(flow.exchange)
    if flow.netflow > 0:
        return f"Zincir: {exchange_name} giriş {amount} | satış riski ⚠️"
    if flow.netflow < 0:
        return f"Zincir: {exchange_name} çıkış {amount} | toplama izi 🟢"
    return "Zincir: Net akış yatay"


def _fake_risk_score(
    item: TimeframeAnalysis,
    order_book: OrderBookPressure | None,
    onchain_flow: OnChainFlow | None = None,
) -> int:
    score = 0
    if item.fake_rise_risk:
        score += 2
    if item.distribution_risk:
        score += 2
    if item.change_pct > 0 and item.taker_delta_pct <= -10:
        score += 1
    if order_book and order_book.imbalance_pct <= -20:
        score += 1
    if (
        order_book
        and order_book.nearest_sell_wall_quote > 0
        and order_book.nearest_sell_wall_quote > order_book.nearest_buy_wall_quote * 1.5
    ):
        score += 1
    if _onchain_sell_pressure(onchain_flow) and item.change_pct >= 0:
        score += 2
    return score


def _fake_or_sell_pressure_reason(
    item_15m: TimeframeAnalysis,
    item_1h: TimeframeAnalysis,
) -> str:
    frames = (item_15m, item_1h)
    falling_pressure = any(
        item.change_pct < 0
        and (
            item.trend_score <= -3
            or item.close <= item.bollinger_lower
            or item.taker_delta_pct <= -10
        )
        for item in frames
    )
    suspicious_rise = any(
        (item.fake_rise_risk or item.distribution_risk)
        and item.change_pct >= 0
        for item in frames
    )

    if falling_pressure and not suspicious_rise:
        return "satış baskısı"
    return "fake/dağıtım riski"


def _footprint_line(
    symbol_analysis: SymbolAnalysis,
    order_book: OrderBookPressure | None,
    onchain_flow: OnChainFlow | None = None,
) -> str | None:
    frames = _timeframe_map(symbol_analysis)
    item = frames.get("15m") or frames.get("1h")
    if not item:
        return None

    if item.taker_buy_ratio >= 50:
        side = f"Alıcı %{item.taker_buy_ratio:.0f}"
    else:
        side = f"Satıcı %{100 - item.taker_buy_ratio:.0f}"

    signals: list[str] = []
    if item.volume_change_pct >= 80 and item.taker_delta_pct <= -15:
        signals.append("Büyük satış izi")
    elif item.volume_change_pct >= 80 and item.taker_delta_pct >= 15:
        signals.append("Büyük alım izi")
    elif item.distribution_risk:
        signals.append("Dağıtım riski")
    elif item.fake_rise_risk:
        signals.append("Fake risk")

    if state := _orderbook_state(order_book):
        signals.append(state)

    if _onchain_sell_pressure(onchain_flow):
        signals.append("Zincir satış baskısı")
    elif _onchain_accumulation(onchain_flow):
        signals.append("Zincir birikim")

    if _fake_risk_score(item, order_book, onchain_flow) >= 3:
        signals.append("Fake YÜKSEK ⚠️")

    return "İz: " + " | ".join([side, *signals])


def _position_line(
    symbol_analysis: SymbolAnalysis,
    altcoin_blocked: bool,
    order_book: OrderBookPressure | None,
    onchain_flow: OnChainFlow | None = None,
    stablecoin_reserve: StablecoinReserve | None = None,
) -> str | None:
    frames = _timeframe_map(symbol_analysis)
    item_15m = frames.get("15m")
    item_1h = frames.get("1h")
    if not item_15m or not item_1h:
        return None

    fake_score = max(
        _fake_risk_score(item_15m, order_book, onchain_flow),
        _fake_risk_score(item_1h, order_book, onchain_flow),
    )
    orderbook_weak = bool(order_book and order_book.imbalance_pct <= -20)
    onchain_weak = _onchain_sell_pressure(onchain_flow)
    onchain_strong = _onchain_accumulation(onchain_flow)
    stablecoin_strong = _stablecoin_buy_power(stablecoin_reserve)

    if altcoin_blocked:
        return "Pozisyon: ZARAR RİSKİ 🔴 | BTC zayıf"
    if fake_score >= 3:
        reason = _fake_or_sell_pressure_reason(item_15m, item_1h)
        if reason == "satış baskısı":
            return "Pozisyon: ZARAR RİSKİ 🔴 | satış baskısı"
        return "Pozisyon: FAKE RİSKİ ⚠️ | kar güveni düşük"
    if onchain_weak and item_1h.trend_score <= 3:
        return "Pozisyon: ZARAR RİSKİ 🔴 | zincir satış baskısı"
    if item_1h.trend_score <= -5 or (item_15m.trend_score <= -5 and item_1h.trend_score <= -3):
        return "Pozisyon: ZARAR RİSKİ 🔴 | trend düşüşte"
    if item_15m.taker_delta_pct <= -15 and item_1h.trend_score <= 0:
        return "Pozisyon: ZARAR RİSKİ 🔴 | satıcı baskısı"
    if (
        item_1h.trend_score >= 6
        and item_15m.trend_score >= 3
        and item_15m.taker_delta_pct >= 0
        and not orderbook_weak
        and not onchain_weak
    ):
        return "Pozisyon: KÂR BEKLENTİSİ 🟢 | trend güçlü"
    if (
        item_1h.trend_score >= 3
        and item_15m.trend_score >= 0
        and item_15m.taker_delta_pct >= -5
        and not onchain_weak
    ):
        if onchain_strong or stablecoin_strong:
            return "Pozisyon: KÂR BEKLENTİSİ 🟢 | zincir destekli"
        return "Pozisyon: KÂR BEKLENTİSİ 🟢 | trend toparlıyor"
    return "Pozisyon: BELİRSİZ 🟡 | izle"


def _entry_decision(
    symbol_analysis: SymbolAnalysis,
    altcoin_blocked: bool,
    order_book: OrderBookPressure | None,
    flow_stats: dict[str, MarketStat] | None,
    onchain_flow: OnChainFlow | None = None,
    stablecoin_reserve: StablecoinReserve | None = None,
) -> tuple[str, bool]:
    frames = _timeframe_map(symbol_analysis)
    item_15m = frames.get("15m")
    item_1h = frames.get("1h")
    item_4h = frames.get("4h")
    if not item_15m or not item_1h or not item_4h:
        return "Giriş: HAYIR 🔴 | veri eksik", False

    flow = flow_stats.get(symbol_analysis.symbol) if flow_stats else None
    flow_amount = _flow_pressure_usd(flow)
    fake_score = max(
        _fake_risk_score(item_15m, order_book, onchain_flow),
        _fake_risk_score(item_1h, order_book, onchain_flow),
    )
    orderbook_weak = bool(order_book and order_book.imbalance_pct <= -10)
    onchain_weak = _onchain_sell_pressure(onchain_flow)
    onchain_strong = _onchain_accumulation(onchain_flow)
    stablecoin_strong = _stablecoin_buy_power(stablecoin_reserve)
    near_support_pct = ((item_4h.close - item_4h.support) / item_4h.support) * 100 if item_4h.support else 999
    reversal = item_15m.candle_pattern in {"Hammer", "Bullish Engulfing"} or item_1h.candle_pattern in {
        "Hammer",
        "Bullish Engulfing",
    }

    if altcoin_blocked:
        return "Giriş: HAYIR 🔴 | BTC zayıf", False
    if fake_score >= 3:
        reason = _fake_or_sell_pressure_reason(item_15m, item_1h)
        return f"Giriş: HAYIR 🔴 | {reason}", False
    if onchain_weak and item_1h.trend_score <= 3:
        return "Giriş: HAYIR 🔴 | zincir satış baskısı", False
    if item_1h.trend_score <= -5 or (item_15m.trend_score <= -5 and item_1h.trend_score <= -3):
        return "Giriş: HAYIR 🔴 | trend düşüşte", False
    if item_15m.rsi >= 72 or item_1h.rsi >= 72:
        return "Giriş: HAYIR 🔴 | RSI yüksek", False
    if item_15m.taker_delta_pct <= -15 and item_1h.trend_score <= 0:
        return "Giriş: HAYIR 🔴 | satıcı baskısı", False
    if flow_amount is not None and flow_amount <= -50_000:
        return f"Giriş: HAYIR 🔴 | para çıkışı {_fmt_amount(flow_amount, '$')}", False
    if item_4h.close <= item_4h.support:
        return "Giriş: HAYIR 🔴 | destek kırıldı", False

    if (
        item_4h.trend_score >= 0
        and item_1h.trend_score >= 7
        and item_15m.trend_score >= 4
        and item_15m.taker_delta_pct >= 5
        and not orderbook_weak
        and not onchain_weak
        and (flow_amount is None or flow_amount >= 50_000)
    ):
        reason = "trend güçlü"
        if onchain_strong or stablecoin_strong:
            reason = "zincir destekli"
        return f"Giriş: EVET 🟢 | {reason}", True

    if (
        near_support_pct <= 1.5
        and item_4h.trend_score >= -2
        and item_1h.trend_score >= 0
        and 32 <= item_15m.rsi <= 52
        and reversal
        and not orderbook_weak
        and (flow_amount is None or flow_amount >= -25_000)
    ):
        return "Giriş: KADEMELİ 🟢 | destekte dönüş var", True

    if item_1h.trend_score >= 4 and item_15m.trend_score >= 1 and item_15m.taker_delta_pct >= 0:
        if flow_amount is not None and flow_amount < 25_000:
            return "Giriş: BEKLE 🟡 | akış zayıf", False
        return "Giriş: BEKLE 🟡 | teyit bekle", False

    return "Giriş: BEKLE 🟡 | net sinyal yok", False


def _timeframe_map(symbol_analysis: SymbolAnalysis) -> dict[str, TimeframeAnalysis]:
    return {item.timeframe: item for item in symbol_analysis.timeframes}


def _component_weight(optimized_weights, market_regime: MarketRegimeResult | None, component: str) -> float:
    if optimized_weights is None or not hasattr(optimized_weights, "component_weight"):
        return 1.0
    regime = market_regime.regime.value if market_regime else None
    return float(optimized_weights.component_weight(component, regime))


def _weighted_points(
    points: int | float,
    optimized_weights,
    market_regime: MarketRegimeResult | None,
    component: str,
) -> int:
    return int(round(points * _component_weight(optimized_weights, market_regime, component)))


def _macd_bias(item_15m: TimeframeAnalysis, item_1h: TimeframeAnalysis) -> int:
    bias = 0
    bias += 3 if item_15m.macd_histogram > 0 else -3
    bias += 4 if item_1h.macd_histogram > 0 else -4
    return bias


def _regime_frame_from_timeframe(item: TimeframeAnalysis) -> RegimeFrame:
    volatility_pct = 0.0
    if item.bollinger_middle > 0:
        volatility_pct = ((item.bollinger_upper - item.bollinger_lower) / item.bollinger_middle) * 100
    return RegimeFrame(
        timeframe=item.timeframe,
        close=item.close,
        ema50=item.ema50,
        ema200=item.ema200,
        ema50_slope_pct=item.ema50_slope_pct,
        volatility_pct=volatility_pct,
        trend_score=item.trend_score,
        rsi=item.rsi,
        momentum_pct=item.momentum_pct,
        support=item.support,
        resistance=item.resistance,
    )


def _market_regime_from_analyses(
    analyses: list[SymbolAnalysis],
    market_context: MarketContext | None,
) -> MarketRegimeResult:
    btc = next((analysis for analysis in analyses if analysis.symbol == "BTCUSDT"), None)
    btc_dominance_pct = market_context.btc_dominance_pct if market_context else None
    if not btc:
        return classify_market_regime(None, btc_dominance_pct=btc_dominance_pct)

    frames = _timeframe_map(btc)
    frame_4h = (
        _regime_frame_from_timeframe(item_4h)
        if (item_4h := frames.get("4h"))
        else None
    )
    frame_1d = (
        _regime_frame_from_timeframe(item_1d)
        if (item_1d := frames.get("1d"))
        else None
    )
    return classify_market_regime(frame_4h, frame_1d, btc_dominance_pct)


def _rise_signal(
    symbol_analysis: SymbolAnalysis,
    flow_stats: dict[str, MarketStat] | None,
    confirm_stats: dict[str, MarketStat] | None,
    altcoin_blocked: bool,
) -> str | None:
    symbol = symbol_analysis.symbol
    frames = _timeframe_map(symbol_analysis)
    item_15m = frames.get("15m")
    item_1h = frames.get("1h")
    item_4h = frames.get("4h")
    flow = flow_stats.get(symbol) if flow_stats else None
    confirm = confirm_stats.get(symbol) if confirm_stats else None
    if not item_15m or not item_1h or not item_4h or not flow:
        return None

    flow_positive = flow.price_change_pct >= 0.15 and flow.quote_volume > 0
    confirm_positive = bool(confirm and confirm.price_change_pct > 0)
    rsi_ok = item_15m.rsi < 70 and item_1h.rsi < 70
    trend_ok = item_1h.trend_score >= 3 or item_4h.trend_score >= 3
    near_breakout = item_15m.close >= item_15m.resistance * 0.995
    momentum_ok = item_15m.momentum_pct > 0 or item_1h.momentum_pct > 0

    if altcoin_blocked:
        if (flow_positive or confirm_positive) and rsi_ok and item_15m.trend_score >= 3 and item_1h.trend_score >= 3:
            return "Yükseliş var ama BTC zayıf ⚠️"
        if near_breakout and momentum_ok and rsi_ok:
            return "Kırılım yaklaşıyor ama BTC zayıf ⚠️"
        return None

    if (item_15m.fake_rise_risk or item_15m.distribution_risk) and flow.price_change_pct > 0:
        return "Yükseliş fake olabilir ⚠️"
    if flow_positive and rsi_ok and item_15m.trend_score >= 3 and item_1h.trend_score >= 3:
        return "Trend erken güçleniyor 🟢"
    if flow_positive and rsi_ok and (confirm_positive or trend_ok or near_breakout):
        return "Yükseliş sinyali var 🟢"
    if near_breakout and momentum_ok and rsi_ok:
        return "Kırılım yaklaşıyor 🟢"
    return None


def _day_trade_levels(symbol_analysis: SymbolAnalysis) -> tuple[float, float, float, list[float]] | None:
    frames = _timeframe_map(symbol_analysis)
    item_15m = frames.get("15m")
    item_1h = frames.get("1h")
    item_4h = frames.get("4h")
    if not item_15m or not item_1h or not item_4h:
        return None

    close = item_15m.close
    raw_levels = [
        item_15m.support,
        item_15m.resistance,
        item_1h.support,
        item_1h.resistance,
        item_4h.support,
        item_4h.resistance,
    ]
    levels = sorted({level for level in raw_levels if level > 0})
    below = [level for level in levels if level < close * 0.999]
    above = [level for level in levels if level > close * 1.001]
    support = below[-1] if below else min(levels)
    resistance = above[0] if above else max(levels)
    if resistance <= close:
        resistance = close * 1.012
    return close, support, resistance, levels


def _day_trade_risk_pct(symbol: str) -> float:
    if symbol == "BTCUSDT":
        return 0.008
    if symbol in {"ETHUSDT", "SOLUSDT"}:
        return 0.012
    return 0.018


def _day_trade_target(entry: float, levels: list[float], risk_pct: float) -> float:
    min_target = entry * (1 + risk_pct * 1.25)
    preferred = [level for level in levels if level > min_target]
    if preferred:
        return preferred[0]
    return entry * (1 + risk_pct * 1.8)


def _day_trade_stop(entry: float, support: float, risk_pct: float) -> float:
    support_stop = support * 0.996
    max_loss_stop = entry * (1 - risk_pct)
    stop = max(support_stop, max_loss_stop)
    if stop >= entry:
        stop = max_loss_stop
    return stop


def _forecast_line(
    symbol_analysis: SymbolAnalysis,
    altcoin_blocked: bool,
    entry_allowed: bool,
    flow_stats: dict[str, MarketStat] | None,
    order_book: OrderBookPressure | None,
) -> str | None:
    frames = _timeframe_map(symbol_analysis)
    item_15m = frames.get("15m")
    item_1h = frames.get("1h")
    day_levels = _day_trade_levels(symbol_analysis)
    if not item_15m or not item_1h or not day_levels:
        return None

    close, support, resistance, levels = day_levels
    flow = flow_stats.get(symbol_analysis.symbol) if flow_stats else None
    flow_amount = _flow_pressure_usd(flow)
    orderbook_weak = bool(order_book and order_book.imbalance_pct <= -20)
    risk_pct = _day_trade_risk_pct(symbol_analysis.symbol)
    target_entry = close if entry_allowed else resistance * 1.001
    target = _day_trade_target(target_entry, levels, risk_pct)
    sell_pressure = item_1h.trend_score <= -3 or orderbook_weak or (
        flow_amount is not None and flow_amount <= -100_000
    )
    buy_pressure = entry_allowed or (
        item_1h.trend_score >= 3
        and item_15m.taker_delta_pct >= -5
        and flow_amount is not None
        and flow_amount >= 25_000
    )

    if altcoin_blocked:
        return f"Beklenti: BTC zayıf | destek testi {_fmt_price(support)}"
    if close <= support:
        return f"Beklenti: destek altı zayıf | toparlanma {_fmt_price(support)} üstü"
    if buy_pressure:
        return f"Beklenti: yukarı deneme | hedef {_fmt_price(target)}"
    if sell_pressure:
        return f"Beklenti: aşağı baskı | destek {_fmt_price(support)}"
    if close >= resistance * 0.99:
        return f"Beklenti: kırılım izlenir | üstü {_fmt_price(resistance)}"
    return f"Beklenti: yatay | {_fmt_price(support)}-{_fmt_price(resistance)}"


def _day_trade_plan(
    symbol_analysis: SymbolAnalysis,
    altcoin_blocked: bool,
    entry_allowed: bool = False,
) -> str:
    day_levels = _day_trade_levels(symbol_analysis)
    if not day_levels:
        return "Trade: veri eksik"

    close, support, resistance, levels = day_levels
    risk_pct = _day_trade_risk_pct(symbol_analysis.symbol)
    near_support = close <= support * 1.012
    near_resistance = close >= resistance * 0.992
    trigger_value = resistance * 1.001

    if entry_allowed and near_support:
        entry_value = close
        entry_high = min(close, support * 1.008)
        entry_text = f"{_fmt_price(support)}-{_fmt_price(entry_high)}"
        target = _day_trade_target(entry_value, levels, risk_pct)
        stop = _day_trade_stop(entry_value, support, risk_pct)
        return (
            f"Trade: Hazır | Giriş Bölgesi {entry_text} | "
            f"Hedef Fiyatı {_fmt_price(target)} | Stop {_fmt_price(stop)}"
        )
    elif entry_allowed and not near_resistance:
        entry_value = close
        target = _day_trade_target(entry_value, levels, risk_pct)
        stop = _day_trade_stop(entry_value, support, risk_pct)
        return (
            f"Trade: Hazır | Giriş Fiyatı {_fmt_price(close)} | "
            f"Hedef Fiyatı {_fmt_price(target)} | Stop {_fmt_price(stop)}"
        )

    target = _day_trade_target(trigger_value, levels, risk_pct)
    stop = _day_trade_stop(trigger_value, support, risk_pct)
    pullback_high = min(close, support * 1.008)
    pullback = f" | Geri Çekilme {_fmt_price(support)}-{_fmt_price(pullback_high)}" if pullback_high > support else ""
    return (
        f"Trade: Bekle | Tetik {_fmt_price(trigger_value)} üstü{pullback} | "
        f"Hedef Fiyatı {_fmt_price(target)} | Stop {_fmt_price(stop)} (tetikten sonra)"
    )


def _simple_market_line(
    crash_text: str | None,
    btc_4h_bearish: bool,
    btc_unavailable: bool,
) -> str:
    if btc_unavailable:
        return "Piyasa: veri yok, bekle 🟡"
    if crash_text and "Çöküş riski YÜKSEK" in crash_text:
        return "Piyasa: çok riskli 🔴"
    if btc_4h_bearish or crash_text:
        return "Piyasa: riskli, bekle 🟡"
    return "Piyasa: normal 🟢"


def _simple_total_flow_line(flow_stats: dict[str, MarketStat] | None) -> str | None:
    totals = _flow_totals(flow_stats)
    if not totals:
        return None

    buy_total, sell_total, net = totals
    if net >= 100_000:
        direction = "giriş 🟢"
    elif net <= -100_000:
        direction = "çıkış 🔴"
    else:
        direction = "kararsız 🟡"
    return (
        f"Para: {direction} {_fmt_amount(net, '$')} | "
        f"Alış {_fmt_abs_amount(buy_total, '$')} / Satış {_fmt_abs_amount(sell_total, '$')}"
    )


def _exchange_spread_emoji(spread_pct: float | None, exchange_count: int) -> str:
    if spread_pct is None or exchange_count < 3:
        return "🟡"
    if spread_pct <= 0.20:
        return "🟢"
    if spread_pct <= 0.60:
        return "🟡"
    return "🔴"


def _exchange_consensus_summary(
    exchange_consensus: dict[str, ExchangeConsensus] | None,
) -> str | None:
    if not exchange_consensus:
        return None

    values = list(exchange_consensus.values())
    total_volume = sum(
        consensus.total_quote_volume_24h or 0.0
        for consensus in values
    )
    active_counts = [consensus.exchange_count for consensus in values]
    spreads = [
        consensus.spread_pct
        for consensus in values
        if consensus.spread_pct is not None
    ]
    if not active_counts:
        return None

    min_count = min(active_counts)
    max_spread = max(spreads) if spreads else None
    emoji = _exchange_spread_emoji(max_spread, min_count)
    if max_spread is None:
        state = "veri sınırlı"
    elif max_spread <= 0.20 and min_count >= 4:
        state = "uyumlu"
    elif max_spread <= 0.60:
        state = "normal fark"
    else:
        state = "fiyat ayrışıyor"

    spread_text = "?" if max_spread is None else f"{max_spread:.2f}%"
    return (
        f"Borsa teyidi: {min_count}+ borsa {state} {emoji} | "
        f"spread {spread_text} | hacim {_fmt_abs_amount(total_volume, '$')}"
    )


def _exchange_consensus_line(consensus: ExchangeConsensus | None) -> str | None:
    if not consensus or consensus.exchange_count < 2:
        return None

    spread = consensus.spread_pct
    emoji = _exchange_spread_emoji(spread, consensus.exchange_count)
    spread_text = "?" if spread is None else f"{spread:.2f}%"
    volume = consensus.total_quote_volume_24h
    volume_text = f" | hacim {_fmt_abs_amount(volume, '$')}" if volume else ""
    return f"Borsa: {consensus.exchange_count} teyit | spread {spread_text} {emoji}{volume_text}"


def _clamp(value: int, low: int = 0, high: int = 100) -> int:
    return max(low, min(high, value))


def _score_emoji(score: int) -> str:
    if score >= 70:
        return "🟢"
    if score >= 45:
        return "🟡"
    return "🔴"


def _rr_emoji(rr: float | None, confidence: int) -> str:
    if rr is None or rr < 1:
        return "🔴"
    if confidence < 45:
        return "🔴"
    if confidence < 70:
        return "🟡"
    if rr >= 2:
        return "🟢"
    return "🟡"


def _risk_reward_ratio(setup: tuple[float, float, float, float, bool] | None) -> float | None:
    if not setup:
        return None
    entry, target, stop, _, _ = setup
    risk = entry - stop
    reward = target - entry
    if risk <= 0 or reward <= 0:
        return 0.0
    return reward / risk


def _news_impact(market_context: MarketContext | None) -> int:
    if not market_context:
        return 0
    return sum(signal.impact for signal in market_context.news_signals)


def _news_filter_line(market_context: MarketContext | None) -> str:
    if not market_context or not market_context.news_signals:
        return "Haber: veri yok 🟡"
    parts = [f"{signal.topic} {signal.label}" for signal in market_context.news_signals[:3]]
    return "Haber: " + " | ".join(parts)


def _btc_dominance_line(market_context: MarketContext | None) -> str | None:
    if not market_context or market_context.btc_dominance_pct is None:
        return None
    return f"BTC.D: {market_context.btc_dominance_pct:.1f}%"


def _market_mode_line(
    crash_text: str | None,
    btc_4h_bearish: bool,
    btc_unavailable: bool,
    confidence_scores: list[int],
    market_context: MarketContext | None,
) -> str:
    confidence = round(sum(confidence_scores) / len(confidence_scores)) if confidence_scores else 0
    news_impact = _news_impact(market_context)

    if (
        btc_unavailable
        or confidence < 35
        or news_impact <= -16
        or (crash_text and "YÜKSEK" in crash_text)
    ):
        mode = "🔴 NAKİTTE KAL"
    elif btc_4h_bearish or crash_text or confidence < 60 or news_impact < -4:
        mode = "🟡 DİKKAT"
    else:
        mode = "🟢 RİSK AÇIK"

    return f"PİYASA MODU: {mode} | Güven {confidence}/100 {_score_emoji(confidence)}"


def _confidence_score(
    symbol_analysis: SymbolAnalysis,
    entry_allowed: bool,
    altcoin_blocked: bool,
    order_book: OrderBookPressure | None,
    flow_stats: dict[str, MarketStat] | None,
    confirm_stats: dict[str, MarketStat] | None,
    onchain_flow: OnChainFlow | None,
    stablecoin_reserve: StablecoinReserve | None,
    market_context: MarketContext | None,
    rr: float | None,
    optimized_weights=None,
    market_regime: MarketRegimeResult | None = None,
) -> int:
    frames = _timeframe_map(symbol_analysis)
    item_15m = frames.get("15m")
    item_1h = frames.get("1h")
    item_4h = frames.get("4h")
    if not item_15m or not item_1h or not item_4h:
        return 0

    score = 50
    score += _weighted_points(
        item_15m.trend_score * 2 + item_1h.trend_score * 3 + item_4h.trend_score,
        optimized_weights,
        market_regime,
        "ema_trend",
    )
    macd_extra = _macd_bias(item_15m, item_1h)
    score += int(round(macd_extra * (_component_weight(optimized_weights, market_regime, "macd") - 1.0)))
    score += 10 if entry_allowed else -5

    if altcoin_blocked:
        score -= 25

    flow = flow_stats.get(symbol_analysis.symbol) if flow_stats else None
    flow_amount = _flow_pressure_usd(flow)
    if flow_amount is not None:
        if flow_amount >= 500_000:
            score += _weighted_points(10, optimized_weights, market_regime, "volume")
        elif flow_amount >= 100_000:
            score += _weighted_points(6, optimized_weights, market_regime, "volume")
        elif flow_amount <= -500_000:
            score -= _weighted_points(12, optimized_weights, market_regime, "volume")
        elif flow_amount <= -100_000:
            score -= _weighted_points(8, optimized_weights, market_regime, "volume")

    confirm = confirm_stats.get(symbol_analysis.symbol) if confirm_stats else None
    if confirm:
        if confirm.price_change_pct >= 0.3:
            score += _weighted_points(5, optimized_weights, market_regime, "volume")
        elif confirm.price_change_pct <= -0.3:
            score -= _weighted_points(5, optimized_weights, market_regime, "volume")

    fake_score = max(
        _fake_risk_score(item_15m, order_book, onchain_flow),
        _fake_risk_score(item_1h, order_book, onchain_flow),
    )
    score -= fake_score * 6

    if order_book:
        score += _weighted_points(
            int(max(min(order_book.imbalance_pct / 4, 8), -8)),
            optimized_weights,
            market_regime,
            "order_book",
        )

    if _onchain_sell_pressure(onchain_flow):
        score -= 10
    elif _onchain_accumulation(onchain_flow):
        score += 6

    if _stablecoin_buy_power(stablecoin_reserve):
        score += 4

    if item_15m.rsi >= 75:
        score -= _weighted_points(10, optimized_weights, market_regime, "rsi")
    elif 42 <= item_15m.rsi <= 64:
        score += _weighted_points(3, optimized_weights, market_regime, "rsi")

    if rr is not None:
        if rr >= 3:
            score += 8
        elif rr >= 2:
            score += 5
        elif rr < 1:
            score -= 20

    score += _news_impact(market_context)
    if altcoin_blocked:
        score = min(score, 40)
    elif not entry_allowed:
        score = min(score, 55)
    if rr is not None and rr < 1:
        score = min(score, 40)
    return _clamp(score)


def _confidence_line(confidence: int, rr: float | None) -> str:
    rr_text = "?" if rr is None else f"1:{rr:.1f}"
    return f"Güven: {confidence}/100 {_score_emoji(confidence)} | R/R {rr_text} {_rr_emoji(rr, confidence)}"


def _quality_adjustment(
    symbol: str,
    market_stats: dict[str, MarketStat] | None,
    flow_stats: dict[str, MarketStat] | None,
    order_book: OrderBookPressure | None,
) -> tuple[int, str | None]:
    adjustment = 0
    reasons: list[str] = []
    market = market_stats.get(symbol) if market_stats else None
    flow = flow_stats.get(symbol) if flow_stats else None

    if symbol not in {"BTCUSDT", "ETHUSDT", "SOLUSDT"}:
        if market and market.quote_volume < QUALITY_LOW_24H_VOLUME:
            adjustment -= 6
            reasons.append("hacim düşük")
        if flow and flow.quote_volume < QUALITY_LOW_FLOW_VOLUME:
            adjustment -= 4
            reasons.append("akış zayıf")

    if order_book:
        sell_wall_heavy = (
            order_book.nearest_sell_wall_quote > 0
            and order_book.nearest_sell_wall_quote > order_book.nearest_buy_wall_quote * 2
        )
        if order_book.imbalance_pct <= -25 or sell_wall_heavy:
            adjustment -= 6
            reasons.append("satış duvarı")

    if not reasons:
        return 0, None
    label = ", ".join(reasons[:2])
    return max(adjustment, -12), f"Kalite: {label} 🔴"


def _fake_pump_line(
    symbol_analysis: SymbolAnalysis,
    order_book: OrderBookPressure | None,
    onchain_flow: OnChainFlow | None,
) -> str | None:
    frames = _timeframe_map(symbol_analysis)
    item_15m = frames.get("15m")
    item_1h = frames.get("1h")
    if not item_15m or not item_1h:
        return None

    score = max(
        _fake_risk_score(item_15m, order_book, onchain_flow),
        _fake_risk_score(item_1h, order_book, onchain_flow),
    )
    if score >= 3:
        return "Fake pump: Yüksek 🔴"
    if score >= 1:
        return "Fake pump: Orta 🟡"
    return None


def _reentry_zone(close: float, support: float, levels: list[float]) -> str:
    lower_levels = [level for level in levels if level < close * 0.998]
    base = lower_levels[-1] if lower_levels else support
    low = base * 0.996
    high = min(base * 1.008, close * 0.998)
    if high <= low:
        return _fmt_price(base)
    return f"{_fmt_price(low)}-{_fmt_price(high)}"


def _profit_guard_line(
    symbol_analysis: SymbolAnalysis,
    flow_stats: dict[str, MarketStat] | None,
    confirm_stats: dict[str, MarketStat] | None,
    order_book: OrderBookPressure | None,
) -> str | None:
    frames = _timeframe_map(symbol_analysis)
    item_15m = frames.get("15m")
    item_1h = frames.get("1h")
    day_levels = _day_trade_levels(symbol_analysis)
    if not item_15m or not item_1h or not day_levels:
        return None

    close, support, resistance, levels = day_levels
    flow = flow_stats.get(symbol_analysis.symbol) if flow_stats else None
    confirm = confirm_stats.get(symbol_analysis.symbol) if confirm_stats else None
    flow_amount = _flow_pressure_usd(flow)
    buy_wall = order_book.nearest_buy_wall_quote if order_book else 0.0
    sell_wall = order_book.nearest_sell_wall_quote if order_book else 0.0
    reentry = _reentry_zone(close, support, levels)
    continuation = resistance * 1.003

    recent_rise = (
        item_15m.change_pct >= 0.45
        or item_15m.momentum_pct >= 1.8
        or item_1h.change_pct >= 1.0
        or item_1h.momentum_pct >= 2.5
    )
    near_resistance = close >= resistance * 0.988 or item_15m.recent_high >= resistance
    upper_band = item_15m.close >= item_15m.bollinger_upper * 0.995
    failed_breakout = item_15m.recent_high >= resistance and item_15m.close < resistance
    sell_wall_heavy = sell_wall > 0 and sell_wall > max(buy_wall, 1.0) * 1.35
    selling_started = (
        (flow_amount is not None and flow_amount <= -25_000)
        or item_15m.taker_delta_pct <= -8
        or item_1h.taker_delta_pct <= -6
        or (order_book is not None and order_book.imbalance_pct <= -10)
        or sell_wall_heavy
    )
    overheated = (
        item_15m.rsi >= 68
        or item_1h.rsi >= 68
        or item_15m.fake_rise_risk
        or item_1h.fake_rise_risk
        or item_15m.distribution_risk
        or item_1h.distribution_risk
    )
    confirm_weak = bool(confirm and confirm.price_change_pct <= -0.15)

    score = 0
    score += 1 if near_resistance else 0
    score += 1 if upper_band else 0
    score += 1 if failed_breakout else 0
    score += 1 if selling_started else 0
    score += 1 if overheated else 0
    score += 1 if confirm_weak else 0

    if recent_rise and score >= 4:
        if failed_breakout or selling_started:
            return f"VARSA ÇIKIŞ 🔴 | tekrar alım {reentry}"
        return f"KÂR KORU 🔴 | yükseliş yoruldu, alım {reentry}"
    if recent_rise and score >= 3:
        return f"KISMİ ÇIKIŞ 🟡 | devam {_fmt_price(continuation)} üstü"
    return None


def _big_order_line(order_book: OrderBookPressure | None) -> str | None:
    if not order_book:
        return None

    buy_quote = order_book.nearest_buy_wall_quote or order_book.bid_quote_1pct
    sell_quote = order_book.nearest_sell_wall_quote or order_book.ask_quote_1pct
    if buy_quote <= 0 and sell_quote <= 0:
        return None

    if sell_quote > buy_quote * 1.25:
        emoji = "🔴"
    elif buy_quote > sell_quote * 1.25:
        emoji = "🟢"
    else:
        return None
    return f"Büyük emir: Alış {_fmt_abs_amount(buy_quote, '$')} / Satış {_fmt_abs_amount(sell_quote, '$')} {emoji}"


def _protection_line(
    setup: tuple[float, float, float, float, bool] | None,
    close: float,
    entry_allowed: bool,
) -> str | None:
    if not setup:
        return None
    entry, target, stop, _, _ = setup
    if entry <= 0:
        return None

    risk_pct = ((entry - stop) / entry) * 100
    reward_pct = ((target - entry) / entry) * 100
    if not entry_allowed:
        return "Koruma: işlem yok, zarar korunur 🟢"

    if target > entry and close > entry:
        progress = ((close - entry) / (target - entry)) * 100
        if progress >= 70:
            return "Koruma: hedefe yakın, kâr koru 🟢"

    return f"Koruma: risk -{risk_pct:.1f}% | ödül +{reward_pct:.1f}%"


def _simple_decision_line(entry_line: str) -> str:
    text = entry_line.removeprefix("Giriş: ").strip()
    parts = [part.strip() for part in text.split("|", 1)]
    status = parts[0]
    reason = parts[1] if len(parts) > 1 else ""

    if "HAYIR" in status:
        decision = "GİRİŞ YOK 🔴"
    elif "EVET" in status or "KADEMELİ" in status:
        decision = "GİRİŞ VAR 🟢"
    elif "BEKLE" in status:
        decision = "BEKLE 🟡"
    else:
        decision = status

    return f"Karar: {decision}" + (f" | {reason}" if reason else "")


def _is_no_trade_entry(entry_line: str) -> bool:
    text = entry_line.upper()
    blockers = (
        "HAYIR",
        "HABER RİSKİ",
        "GÜVEN DÜŞÜK",
        "R/R DÜŞÜK",
    )
    return any(blocker in text for blocker in blockers)


def _simple_symbol_flow(symbol: str, flow_stats: dict[str, MarketStat] | None) -> str | None:
    flow = flow_stats.get(symbol) if flow_stats else None
    amount = _flow_pressure_usd(flow)
    if amount is None:
        return None

    if amount >= 25_000:
        label = "alış 🟢"
    elif amount <= -25_000:
        label = "satış 🔴"
    else:
        label = "zayıf 🟡"
    return f"Para: {_fmt_amount(amount, '$')} {label}"


def _rocket_line(
    symbol_analysis: SymbolAnalysis,
    flow_stats: dict[str, MarketStat] | None,
    confirm_stats: dict[str, MarketStat] | None,
    order_book: OrderBookPressure | None,
    entry_allowed: bool,
    altcoin_blocked: bool,
) -> str | None:
    if altcoin_blocked:
        return None

    frames = _timeframe_map(symbol_analysis)
    item_15m = frames.get("15m")
    item_1h = frames.get("1h")
    day_levels = _day_trade_levels(symbol_analysis)
    flow = flow_stats.get(symbol_analysis.symbol) if flow_stats else None
    confirm = confirm_stats.get(symbol_analysis.symbol) if confirm_stats else None
    if not item_15m or not item_1h or not day_levels or not flow:
        return None

    close, _, resistance, _ = day_levels
    flow_amount = _flow_pressure_usd(flow)
    near_breakout = close >= resistance * 0.992
    strong_flow = flow_amount is not None and flow_amount >= 100_000
    trend_ready = item_15m.trend_score >= 3 and item_1h.trend_score >= 3
    rsi_ok = item_15m.rsi < 72 and item_1h.rsi < 72
    confirm_ok = not confirm or confirm.price_change_pct >= -0.10
    orderbook_ok = not order_book or order_book.imbalance_pct > -15
    fake_risk = item_15m.fake_rise_risk or item_15m.distribution_risk

    if entry_allowed and near_breakout and strong_flow and trend_ready and rsi_ok and confirm_ok and orderbook_ok and not fake_risk:
        return "🚀 Ekstra yükseliş yakın"
    return None


def _strong_rise_alert_line(
    symbol_analysis: SymbolAnalysis,
    flow_stats: dict[str, MarketStat] | None,
    confirm_stats: dict[str, MarketStat] | None,
    order_book: OrderBookPressure | None,
    entry_allowed: bool,
    altcoin_blocked: bool,
) -> str | None:
    if altcoin_blocked or not entry_allowed:
        return None

    frames = _timeframe_map(symbol_analysis)
    item_15m = frames.get("15m")
    item_1h = frames.get("1h")
    item_4h = frames.get("4h")
    flow = flow_stats.get(symbol_analysis.symbol) if flow_stats else None
    confirm = confirm_stats.get(symbol_analysis.symbol) if confirm_stats else None
    if not item_15m or not item_1h or not item_4h or not flow:
        return None

    flow_amount = _flow_pressure_usd(flow)
    fake_score = max(
        _fake_risk_score(item_15m, order_book),
        _fake_risk_score(item_1h, order_book),
    )
    trend_strong = item_15m.trend_score >= 5 and item_1h.trend_score >= 5 and item_4h.trend_score >= 0
    ema_ok = (
        item_15m.close >= item_15m.ema20
        and item_15m.close >= item_15m.ema50
        and item_1h.close >= item_1h.ema20
    )
    rsi_ok = item_15m.rsi < 72 and item_1h.rsi < 72
    flow_ok = flow_amount is not None and flow_amount >= 100_000
    confirm_ok = confirm is None or confirm.price_change_pct >= 0.20
    orderbook_ok = order_book is None or order_book.imbalance_pct > -10

    if trend_strong and ema_ok and rsi_ok and flow_ok and confirm_ok and orderbook_ok and fake_score <= 1:
        return "GÜÇLÜ YÜKSELİŞ ALARMI 🟢🚀"
    return None


def _dip_reversal_line(
    symbol_analysis: SymbolAnalysis,
    flow_stats: dict[str, MarketStat] | None,
    confirm_stats: dict[str, MarketStat] | None,
    order_book: OrderBookPressure | None,
    altcoin_blocked: bool,
) -> str | None:
    frames = _timeframe_map(symbol_analysis)
    item_15m = frames.get("15m")
    item_1h = frames.get("1h")
    item_4h = frames.get("4h")
    day_levels = _day_trade_levels(symbol_analysis)
    if not item_15m or not item_1h or not item_4h or not day_levels:
        return None

    close, support, resistance, _ = day_levels
    if support <= 0:
        return None

    flow = flow_stats.get(symbol_analysis.symbol) if flow_stats else None
    confirm = confirm_stats.get(symbol_analysis.symbol) if confirm_stats else None
    flow_amount = _flow_pressure_usd(flow)
    near_support_pct = ((close - support) / support) * 100
    support_broken = close <= support or item_15m.close <= item_15m.support or item_1h.close <= item_1h.support
    near_support = 0 <= near_support_pct <= 1.8
    oversold = item_15m.rsi <= 40 or item_1h.rsi <= 42 or item_4h.rsi <= 42
    reversal_candle = item_15m.candle_pattern in {"Hammer", "Bullish Engulfing"} or item_1h.candle_pattern in {
        "Hammer",
        "Bullish Engulfing",
    }
    fake_score = max(
        _fake_risk_score(item_15m, order_book),
        _fake_risk_score(item_1h, order_book),
    )
    orderbook_weak = bool(order_book and order_book.imbalance_pct <= -20)
    selling_pressure = (
        item_15m.taker_delta_pct <= -12
        or item_1h.trend_score <= -5
        or (flow_amount is not None and flow_amount <= -50_000)
        or orderbook_weak
    )
    selling_slowed = (
        item_15m.taker_delta_pct > -10
        and (flow_amount is None or flow_amount > -50_000)
        and fake_score <= 2
        and not orderbook_weak
    )
    confirmation_flow = (
        flow_amount is not None
        and flow_amount >= 25_000
        and (confirm is None or confirm.price_change_pct >= -0.10)
    )
    turn_confirmed = (
        close > support
        and item_15m.close >= item_15m.ema20
        and item_15m.trend_score >= 3
        and item_1h.trend_score >= 0
        and confirmation_flow
        and fake_score <= 1
        and not orderbook_weak
    )

    if support_broken and selling_pressure:
        return f"DÜŞEN BIÇAK 🔴 | destek {_fmt_price(support)} kırıldı"
    if turn_confirmed:
        if altcoin_blocked:
            return "DÖNÜŞ VAR ama BTC zayıf ⚠️"
        return f"DÖNÜŞ TEYİDİ 🟢 | destek {_fmt_price(support)} korundu"
    if near_support and oversold and selling_slowed:
        if altcoin_blocked:
            return f"DİP BÖLGESİ 🟡 | BTC zayıf, destek {_fmt_price(support)}"
        if reversal_candle:
            return f"DİP BÖLGESİ 🟡 | kademeli izle, destek {_fmt_price(support)}"
        return f"DİP YAKIN 🟡 | teyit {_fmt_price(resistance)} üstü"
    return None


def _trade_setup_values(
    symbol_analysis: SymbolAnalysis,
    entry_allowed: bool,
) -> tuple[float, float, float, float, bool] | None:
    day_levels = _day_trade_levels(symbol_analysis)
    if not day_levels:
        return None

    close, support, resistance, levels = day_levels
    risk_pct = _day_trade_risk_pct(symbol_analysis.symbol)
    near_support = close <= support * 1.012
    near_resistance = close >= resistance * 0.992

    if entry_allowed and near_support:
        entry_value = close
        needs_trigger = False
    elif entry_allowed and not near_resistance:
        entry_value = close
        needs_trigger = False
    else:
        entry_value = resistance * 1.001
        needs_trigger = True

    target = _day_trade_target(entry_value, levels, risk_pct)
    stop = _day_trade_stop(entry_value, support, risk_pct)
    return entry_value, target, stop, support, needs_trigger


def _real_trade_check_line(
    symbol_analysis: SymbolAnalysis,
    entry_allowed: bool,
) -> str | None:
    frames = _timeframe_map(symbol_analysis)
    item_15m = frames.get("15m")
    setup = _trade_setup_values(symbol_analysis, entry_allowed)
    if not item_15m or not setup:
        return None

    entry_value, target, stop, _, needs_trigger = setup
    close = item_15m.close
    recent_high = item_15m.recent_high
    recent_low = item_15m.recent_low

    if needs_trigger:
        trigger_closed = close >= entry_value
        trigger_touched = recent_high >= entry_value
        if not trigger_touched:
            return None
        if not trigger_closed:
            return f"Gerçek: tetik dokundu, 15m kapanış yok 🟡"
        return f"Gerçek: 15m kapanışla tetik görüldü | sonrası izleniyor"

    if recent_high >= target:
        return f"Gerçek: hedef görüldü 🟢"
    if recent_low <= stop:
        return f"Gerçek: stop bölgesi görüldü 🔴"
    if close > entry_value and target > entry_value:
        progress = ((close - entry_value) / (target - entry_value)) * 100
        if progress >= 50:
            return f"Gerçek: kârda ilerliyor 🟢 | hedef {_fmt_price(target)}"
    if close < entry_value and entry_value > stop:
        risk_used = ((entry_value - close) / (entry_value - stop)) * 100
        if risk_used >= 60:
            return f"Gerçek: stopa yakın 🔴 | stop {_fmt_price(stop)}"
    return f"Gerçek: işlem bekliyor | hedef {_fmt_price(target)}"


def _simple_trade_lines(
    symbol_analysis: SymbolAnalysis,
    entry_allowed: bool,
    entry_line: str | None = None,
) -> list[str]:
    if entry_line and _is_no_trade_entry(entry_line):
        return []

    day_levels = _day_trade_levels(symbol_analysis)
    if not day_levels:
        return ["Giriş: veri eksik"]

    close, support, resistance, levels = day_levels
    risk_pct = _day_trade_risk_pct(symbol_analysis.symbol)
    near_support = close <= support * 1.012
    near_resistance = close >= resistance * 0.992

    if entry_allowed and near_support:
        entry_value = close
        entry_high = min(close, support * 1.008)
        entry = f"{_fmt_price(support)}-{_fmt_price(entry_high)}"
        stop_label = "Stop"
    elif entry_allowed and not near_resistance:
        entry_value = close
        entry = _fmt_price(close)
        stop_label = "Stop"
    else:
        entry_value = resistance * 1.001
        entry = "Bekle"
    stop_label = "Stop"

    target = _day_trade_target(entry_value, levels, risk_pct)
    stop = _day_trade_stop(entry_value, support, risk_pct)
    entry_emoji = "🟢" if entry_allowed else "🟡"
    if not entry_allowed:
        return [f"Giriş: Bekle 🟡 | Tetik {_fmt_price(entry_value)} | Hedef {_fmt_price(target)} | Stop {_fmt_price(stop)}"]

    lines = [f"Giriş: {entry} {entry_emoji} | Hedef {_fmt_price(target)} | Stop {_fmt_price(stop)}"]
    return lines


def _simple_alarm_line(alarm_lines: list[str]) -> str:
    return f"Uyarı: {alarm_lines[0]} ⚠️" if alarm_lines else "Uyarı: Yok 🟢"


def _futures_side_emoji(side: str) -> str:
    if side == "LONG":
        return "🟢"
    if side == "SHORT":
        return "🔴"
    return "🟡"


def _day_trade_short_target(entry: float, levels: list[float], risk_pct: float) -> float:
    max_target = entry * (1 - risk_pct * 1.25)
    preferred = [level for level in reversed(levels) if level < max_target]
    if preferred:
        return preferred[0]
    return entry * (1 - risk_pct * 1.8)


def _day_trade_short_stop(entry: float, resistance: float, risk_pct: float) -> float:
    resistance_stop = resistance * 1.004
    max_loss_stop = entry * (1 + risk_pct)
    stop = min(resistance_stop, max_loss_stop)
    if stop <= entry:
        stop = max_loss_stop
    return stop


def _futures_setup_values(
    symbol_analysis: SymbolAnalysis,
    side: str,
    side_allowed: bool,
) -> tuple[float, float, float, float, bool] | None:
    if side == "LONG":
        return _trade_setup_values(symbol_analysis, side_allowed)

    day_levels = _day_trade_levels(symbol_analysis)
    if side != "SHORT" or not day_levels:
        return None

    close, support, resistance, levels = day_levels
    risk_pct = _day_trade_risk_pct(symbol_analysis.symbol)
    near_support = close <= support * 1.008

    if side_allowed and not near_support:
        entry_value = close
        needs_trigger = False
    else:
        entry_value = support * 0.999
        needs_trigger = True

    target = _day_trade_short_target(entry_value, levels, risk_pct)
    stop = _day_trade_short_stop(entry_value, resistance, risk_pct)
    return entry_value, target, stop, resistance, needs_trigger


def _futures_rr(setup: tuple[float, float, float, float, bool] | None, side: str) -> float | None:
    if not setup:
        return None
    entry, target, stop, _, _ = setup
    if entry <= 0:
        return None
    if side == "LONG":
        risk = entry - stop
        reward = target - entry
    elif side == "SHORT":
        risk = stop - entry
        reward = entry - target
    else:
        return None
    if risk <= 0 or reward <= 0:
        return None
    return reward / risk


def _futures_risk_plan(
    side: str,
    entry: float,
    target: float,
    stop: float,
    min_rr: float,
    confidence: int,
    market_regime: MarketRegimeResult | None = None,
    daily_loss_pct: float | None = None,
    daily_losing_trades: int | None = None,
) -> RiskPlan:
    max_leverage = FUTURES_MAX_LEVERAGE
    if market_regime and market_regime.regime == MarketRegime.LATE_BULL:
        max_leverage = min(max_leverage, 3)
    return calculate_futures_risk(
        side=side,
        entry=entry,
        stop_loss=stop,
        take_profit=target,
        account_balance=FUTURES_ACCOUNT_BALANCE,
        risk_pct=FUTURES_MAX_RISK_PCT,
        confidence=confidence,
        max_leverage=max_leverage,
        min_rr=min_rr,
        max_stop_loss_pct=FUTURES_MAX_STOP_LOSS_PCT,
        daily_loss_pct=FUTURES_TODAY_LOSS_PCT if daily_loss_pct is None else daily_loss_pct,
        daily_losing_trades=(
            FUTURES_TODAY_LOSING_TRADES
            if daily_losing_trades is None
            else daily_losing_trades
        ),
        max_daily_loss_pct=FUTURES_MAX_DAILY_LOSS_PCT,
        max_daily_losing_trades=FUTURES_MAX_DAILY_LOSING_TRADES,
    )


def _futures_range_middle(symbol_analysis: SymbolAnalysis) -> bool:
    day_levels = _day_trade_levels(symbol_analysis)
    if day_levels:
        close, support, resistance, _ = day_levels
        if is_near_middle_of_range(close, support, resistance):
            return True

    frames = _timeframe_map(symbol_analysis)
    item_4h = frames.get("4h")
    if item_4h:
        return is_near_middle_of_range(item_4h.close, item_4h.support, item_4h.resistance)
    return False


def _weighted_futures_flow_scores(
    item_15m: TimeframeAnalysis,
    futures_flow: FuturesFlowSnapshot,
    optimized_weights,
    market_regime: MarketRegimeResult | None,
) -> tuple[int, int]:
    long_score = 0
    short_score = 0
    oi_change_pct = futures_flow.oi_change_pct
    funding_pct = futures_flow.funding_rate_pct
    price_change_pct = item_15m.change_pct
    volume_momentum_pct = item_15m.volume_change_pct

    if price_change_pct >= 0.8 and oi_change_pct is not None and oi_change_pct >= 6:
        short_score += _weighted_points(12, optimized_weights, market_regime, "open_interest")
    if funding_pct is not None and funding_pct >= 0.05:
        short_score += _weighted_points(10, optimized_weights, market_regime, "funding_rate")
    elif funding_pct is not None and funding_pct >= 0.025:
        short_score += _weighted_points(5, optimized_weights, market_regime, "funding_rate")

    if futures_flow.crowding in {"LONG_CROWDED", "TOP_LONG_CROWDED"}:
        short_score += _weighted_points(10, optimized_weights, market_regime, "long_short_ratio")
    if price_change_pct > 0 and volume_momentum_pct <= -10:
        short_score += _weighted_points(6, optimized_weights, market_regime, "volume")

    if futures_flow.crowding in {"SHORT_CROWDED", "TOP_SHORT_CROWDED"}:
        long_score += _weighted_points(12, optimized_weights, market_regime, "long_short_ratio")
    if funding_pct is not None and funding_pct <= -0.05:
        long_score += _weighted_points(10, optimized_weights, market_regime, "funding_rate")
    elif funding_pct is not None and funding_pct <= -0.025:
        long_score += _weighted_points(5, optimized_weights, market_regime, "funding_rate")
    if price_change_pct >= 0.25 and oi_change_pct is not None and 1.5 <= oi_change_pct <= 8:
        long_score += _weighted_points(8, optimized_weights, market_regime, "open_interest")
    if price_change_pct <= -0.25 and oi_change_pct is not None and oi_change_pct <= -1:
        long_score += _weighted_points(4, optimized_weights, market_regime, "open_interest")

    return long_score, short_score


def _futures_direction_scores(
    symbol_analysis: SymbolAnalysis,
    altcoin_blocked: bool,
    order_book: OrderBookPressure | None,
    flow_stats: dict[str, MarketStat] | None,
    confirm_stats: dict[str, MarketStat] | None,
    onchain_flow: OnChainFlow | None,
    stablecoin_reserve: StablecoinReserve | None,
    market_context: MarketContext | None,
    market_regime: MarketRegimeResult | None = None,
    futures_flow: FuturesFlowSnapshot | None = None,
    optimized_weights=None,
) -> tuple[int, int]:
    frames = _timeframe_map(symbol_analysis)
    item_15m = frames.get("15m")
    item_1h = frames.get("1h")
    item_4h = frames.get("4h")
    if not item_15m or not item_1h or not item_4h:
        return 0, 0

    trend_bias = _weighted_points(
        item_15m.trend_score * 2 + item_1h.trend_score * 3 + item_4h.trend_score,
        optimized_weights,
        market_regime,
        "ema_trend",
    )
    long_score = 50 + trend_bias
    short_score = 50 - trend_bias
    macd_extra = int(round(_macd_bias(item_15m, item_1h) * (_component_weight(optimized_weights, market_regime, "macd") - 1.0)))
    long_score += macd_extra
    short_score -= macd_extra

    if altcoin_blocked:
        long_score -= 18
        short_score += 6

    flow = flow_stats.get(symbol_analysis.symbol) if flow_stats else None
    flow_amount = _flow_pressure_usd(flow)
    if flow_amount is not None:
        if flow_amount >= 500_000:
            adjust = _weighted_points(10, optimized_weights, market_regime, "volume")
            long_score += adjust
            short_score -= adjust
        elif flow_amount >= 100_000:
            adjust = _weighted_points(6, optimized_weights, market_regime, "volume")
            long_score += adjust
            short_score -= adjust
        elif flow_amount <= -500_000:
            long_score -= _weighted_points(12, optimized_weights, market_regime, "volume")
            short_score += _weighted_points(10, optimized_weights, market_regime, "volume")
        elif flow_amount <= -100_000:
            long_score -= _weighted_points(8, optimized_weights, market_regime, "volume")
            short_score += _weighted_points(6, optimized_weights, market_regime, "volume")

    confirm = confirm_stats.get(symbol_analysis.symbol) if confirm_stats else None
    if confirm:
        if confirm.price_change_pct >= 0.3:
            adjust = _weighted_points(5, optimized_weights, market_regime, "volume")
            long_score += adjust
            short_score -= adjust
        elif confirm.price_change_pct <= -0.3:
            adjust = _weighted_points(5, optimized_weights, market_regime, "volume")
            long_score -= adjust
            short_score += adjust

    fake_score = max(
        _fake_risk_score(item_15m, order_book, onchain_flow),
        _fake_risk_score(item_1h, order_book, onchain_flow),
    )
    long_score -= fake_score * 7
    if fake_score >= 3 and item_1h.trend_score <= 1:
        short_score += 5

    if order_book:
        ob_adjust = _weighted_points(
            int(max(min(order_book.imbalance_pct / 4, 8), -8)),
            optimized_weights,
            market_regime,
            "order_book",
        )
        long_score += ob_adjust
        short_score -= ob_adjust

    if _onchain_sell_pressure(onchain_flow):
        long_score -= 10
        short_score += 8
    elif _onchain_accumulation(onchain_flow):
        long_score += 6
        short_score -= 6

    if _stablecoin_buy_power(stablecoin_reserve):
        long_score += 4
        short_score -= 4

    if item_15m.rsi >= 75:
        long_score -= _weighted_points(12, optimized_weights, market_regime, "rsi")
        short_score += _weighted_points(4, optimized_weights, market_regime, "rsi")
    elif item_15m.rsi <= 25:
        long_score += _weighted_points(4, optimized_weights, market_regime, "rsi")
        short_score -= _weighted_points(12, optimized_weights, market_regime, "rsi")
    elif 42 <= item_15m.rsi <= 64:
        if trend_bias >= 0:
            long_score += _weighted_points(3, optimized_weights, market_regime, "rsi")
        else:
            short_score += _weighted_points(3, optimized_weights, market_regime, "rsi")

    news_impact = _news_impact(market_context)
    long_score += news_impact
    short_score -= news_impact

    if market_regime:
        if market_regime.regime == MarketRegime.BULL:
            long_score += _weighted_points(10, optimized_weights, market_regime, "market_regime")
            short_score -= _weighted_points(15, optimized_weights, market_regime, "market_regime")
        elif market_regime.regime == MarketRegime.BEAR:
            short_score += _weighted_points(12, optimized_weights, market_regime, "market_regime")
            long_score -= _weighted_points(8, optimized_weights, market_regime, "market_regime")

    if futures_flow:
        flow_long_score, flow_short_score = _weighted_futures_flow_scores(
            item_15m,
            futures_flow,
            optimized_weights,
            market_regime,
        )
        long_score += flow_long_score
        short_score += flow_short_score
        bias_penalty = _weighted_points(4, optimized_weights, market_regime, "long_short_ratio")
        if futures_flow.smart_money_bias == "LONG":
            short_score -= bias_penalty
        elif futures_flow.smart_money_bias == "SHORT":
            long_score -= bias_penalty

    return _clamp(long_score), _clamp(short_score)


def _futures_signal(
    symbol_analysis: SymbolAnalysis,
    altcoin_blocked: bool,
    order_book: OrderBookPressure | None,
    flow_stats: dict[str, MarketStat] | None,
    confirm_stats: dict[str, MarketStat] | None,
    onchain_flow: OnChainFlow | None,
    stablecoin_reserve: StablecoinReserve | None,
    market_context: MarketContext | None,
    market_regime: MarketRegimeResult | None = None,
    daily_loss_pct: float | None = None,
    daily_losing_trades: int | None = None,
    futures_flow: FuturesFlowSnapshot | None = None,
    optimized_weights=None,
) -> FuturesSignal:
    frames = _timeframe_map(symbol_analysis)
    item_15m = frames.get("15m")
    item_1h = frames.get("1h")
    item_4h = frames.get("4h")
    if not item_15m or not item_1h or not item_4h:
        return FuturesSignal(
            "BEKLE",
            "BEKLE",
            0,
            0,
            "veri eksik",
            None,
            None,
            None,
            None,
            market_regime=market_regime,
            futures_flow=futures_flow,
        )

    long_score, short_score = _futures_direction_scores(
        symbol_analysis,
        altcoin_blocked,
        order_book,
        flow_stats,
        confirm_stats,
        onchain_flow,
        stablecoin_reserve,
        market_context,
        market_regime,
        futures_flow,
        optimized_weights,
    )
    flow = flow_stats.get(symbol_analysis.symbol) if flow_stats else None
    flow_amount = _flow_pressure_usd(flow)
    fake_score = max(
        _fake_risk_score(item_15m, order_book, onchain_flow),
        _fake_risk_score(item_1h, order_book, onchain_flow),
    )
    orderbook_weak = bool(order_book and order_book.imbalance_pct <= -15)
    orderbook_strong = bool(order_book and order_book.imbalance_pct >= 15)
    onchain_weak = _onchain_sell_pressure(onchain_flow)
    onchain_strong = _onchain_accumulation(onchain_flow)
    stablecoin_strong = _stablecoin_buy_power(stablecoin_reserve)

    long_trend = (
        item_4h.trend_score >= -1
        and item_1h.trend_score >= 5
        and item_15m.trend_score >= 2
    ) or (
        item_4h.trend_score >= 0
        and item_1h.trend_score >= 3
        and item_15m.trend_score >= 4
    )
    short_trend = (
        item_4h.trend_score <= 1
        and item_1h.trend_score <= -5
        and item_15m.trend_score <= -2
    ) or (
        item_4h.trend_score <= 0
        and item_1h.trend_score <= -3
        and item_15m.trend_score <= -4
    )

    long_blockers: list[str] = []
    if altcoin_blocked:
        long_blockers.append("BTC zayıf")
    if fake_score >= 3:
        long_blockers.append("fake/dağıtım riski")
    if onchain_weak and item_1h.trend_score <= 3:
        long_blockers.append("zincir satış baskısı")
    if item_15m.rsi >= 72 or item_1h.rsi >= 72:
        long_blockers.append("RSI yüksek")
    if item_15m.taker_delta_pct <= -10 and item_1h.trend_score <= 1:
        long_blockers.append("satıcı baskısı")
    if flow_amount is not None and flow_amount <= -50_000:
        long_blockers.append("para çıkışı")
    if orderbook_weak:
        long_blockers.append("satış duvarı")

    short_blockers: list[str] = []
    if item_15m.rsi <= 28 or item_1h.rsi <= 28:
        short_blockers.append("RSI çok düşük")
    if onchain_strong and item_1h.trend_score >= -3:
        short_blockers.append("zincir birikim")
    if stablecoin_strong and item_1h.trend_score >= -3:
        short_blockers.append("stablecoin alım gücü")
    if item_15m.taker_delta_pct >= 10 and item_1h.trend_score >= -1:
        short_blockers.append("alıcı baskısı")
    if flow_amount is not None and flow_amount >= 50_000:
        short_blockers.append("para girişi")
    if orderbook_strong:
        short_blockers.append("alış duvarı")

    range_middle = bool(
        market_regime
        and market_regime.regime == MarketRegime.RANGE
        and _futures_range_middle(symbol_analysis)
    )
    if range_middle:
        long_blockers.append("RANGE orta bant")
        short_blockers.append("RANGE orta bant")

    long_ready = (
        long_score >= FUTURES_MIN_CONFIDENCE
        and long_score >= short_score + FUTURES_MIN_EDGE
        and long_trend
        and not long_blockers
    )
    short_ready = (
        short_score >= FUTURES_MIN_CONFIDENCE
        and short_score >= long_score + FUTURES_MIN_EDGE
        and short_trend
        and not short_blockers
    )

    if long_ready and (not short_ready or long_score >= short_score):
        side = "LONG"
        confidence = long_score
        opposite = short_score
        reason = "trend ve akış yukarı"
    elif short_ready:
        side = "SHORT"
        confidence = short_score
        opposite = long_score
        reason = "trend ve akış aşağı"
    else:
        if long_score >= short_score + FUTURES_MIN_EDGE:
            bias = "LONG"
            blockers = long_blockers
            reason = ", ".join(blockers[:2]) if blockers else "LONG teyidi zayıf"
            confidence = long_score
            opposite = short_score
        elif short_score >= long_score + FUTURES_MIN_EDGE:
            bias = "SHORT"
            blockers = short_blockers
            reason = ", ".join(blockers[:2]) if blockers else "SHORT teyidi zayıf"
            confidence = short_score
            opposite = long_score
        else:
            bias = "BEKLE"
            reason = "net yön yok"
            confidence = max(long_score, short_score)
            opposite = min(long_score, short_score)
        setup = _futures_setup_values(symbol_analysis, bias, False) if bias in {"LONG", "SHORT"} else None
        rr = _futures_rr(setup, bias) if setup else None
        if setup:
            entry, target, stop, _, needs_trigger = setup
            return FuturesSignal(
                "BEKLE",
                bias,
                confidence,
                opposite,
                reason,
                entry,
                target,
                stop,
                rr,
                needs_trigger,
                market_regime,
                futures_flow=futures_flow,
            )
        return FuturesSignal(
            "BEKLE",
            bias,
            confidence,
            opposite,
            reason,
            None,
            None,
            None,
            rr,
            market_regime=market_regime,
            futures_flow=futures_flow,
        )

    setup = _futures_setup_values(symbol_analysis, side, True)
    rr = _futures_rr(setup, side)
    min_rr = max(FUTURES_MIN_RR, market_regime.min_rr if market_regime else FUTURES_MIN_RR)
    if not setup:
        return FuturesSignal(
            "BEKLE",
            side,
            confidence,
            opposite,
            "risk planı yok",
            None,
            None,
            None,
            rr,
            False,
            market_regime,
            futures_flow=futures_flow,
        )

    entry, target, stop, _, needs_trigger = setup
    risk_plan = _futures_risk_plan(
        side,
        entry,
        target,
        stop,
        min_rr,
        confidence,
        market_regime,
        daily_loss_pct,
        daily_losing_trades,
    )
    if risk_plan.status in {RiskStatus.SKIP, RiskStatus.NO_TRADE}:
        reason_prefix = "Risk NO_TRADE" if risk_plan.status == RiskStatus.NO_TRADE else "Risk SKIP"
        return FuturesSignal(
            "BEKLE",
            side,
            confidence,
            opposite,
            f"{reason_prefix}: {risk_plan.reason}",
            entry,
            target,
            stop,
            risk_plan.rr,
            needs_trigger,
            market_regime,
            risk_plan,
            futures_flow,
        )

    return FuturesSignal(
        side,
        side,
        confidence,
        opposite,
        reason,
        entry,
        target,
        stop,
        risk_plan.rr,
        needs_trigger,
        market_regime,
        risk_plan,
        futures_flow,
    )


def _futures_decision_line(signal: FuturesSignal) -> str:
    direction = signal.side if signal.side in {"LONG", "SHORT"} else signal.bias
    direction = direction if direction in {"LONG", "SHORT"} else "NÖTR"
    direction_emoji = _futures_side_emoji(direction)
    regime_suffix = _futures_regime_suffix(signal)

    if signal.side == "BEKLE":
        return (
            f"Futures Yön: {direction} {direction_emoji} | İşlem: BEKLE 🟡 | "
            f"Güven {signal.confidence}/100 {_score_emoji(signal.confidence)} | {signal.reason}"
            f"{regime_suffix}"
        )

    trigger_text = " | tetik bekle" if signal.needs_trigger else ""
    return (
        f"Futures Yön: {signal.side} {direction_emoji} | İşlem: AÇ {signal.side} | "
        f"Güven {signal.confidence}/100 "
        f"{_score_emoji(signal.confidence)} | {signal.reason}{trigger_text}{regime_suffix}"
    )


def _futures_regime_suffix(signal: FuturesSignal) -> str:
    regime = signal.market_regime
    if not regime:
        return ""
    if regime.regime == MarketRegime.LATE_BULL:
        parts = [regime.leverage_note]
        if regime.warning:
            parts.append(regime.warning)
        return " | " + " | ".join(parts)
    if regime.regime == MarketRegime.RANGE:
        return " | RANGE: R/R min 1:2.5 | orta bant yok"
    return ""


def _fmt_optional_pct(value: float | None, decimals: int = 2) -> str:
    if value is None:
        return "?"
    return f"{value:+.{decimals}f}%"


def _fmt_ratio_side(value: float | None) -> str:
    if value is None:
        return "?"
    return f"{value * 100:.0f}%"


def _futures_flow_line(flow: FuturesFlowSnapshot | None) -> str | None:
    if not flow:
        return None
    oi_value = (
        f" | value {_fmt_abs_amount(flow.open_interest_value, '$')}"
        if flow.open_interest_value is not None
        else ""
    )
    global_ratio = "?"
    if flow.global_long_short_ratio is not None:
        global_ratio = f"{flow.global_long_short_ratio:.2f}"
    top_ratio = "?"
    if flow.top_long_short_ratio is not None:
        top_ratio = f"{flow.top_long_short_ratio:.2f}"
    score_text = f"L{flow.long_score}/S{flow.short_score}"
    return "\n".join(
        [
            "Futures Flow:",
            f"OI: {_fmt_optional_pct(flow.oi_change_pct)}{oi_value}",
            f"Funding: {_fmt_optional_pct(flow.funding_rate_pct, 4)}",
            (
                "Long/Short: "
                f"global {_fmt_ratio_side(flow.global_long_pct)}/{_fmt_ratio_side(flow.global_short_pct)} "
                f"({global_ratio}) | top {_fmt_ratio_side(flow.top_long_pct)}/{_fmt_ratio_side(flow.top_short_pct)} "
                f"({top_ratio})"
            ),
            f"Crowding: {flow.crowding}",
            f"Smart Money Bias: {flow.smart_money_bias} | {score_text} | {flow.reason}",
        ]
    )


def _base_asset(symbol: str) -> str:
    return symbol[:-4] if symbol.endswith("USDT") else symbol


def _futures_plan_line(symbol: str, signal: FuturesSignal) -> str | None:
    if signal.entry is None or signal.target is None or signal.stop is None:
        return None
    if signal.side not in {"LONG", "SHORT"} and not signal.risk_plan:
        return None
    rr_text = "?" if signal.rr is None else f"1:{signal.rr:.1f}"
    entry_label = "Tetik" if signal.needs_trigger else "Giriş"
    if not signal.risk_plan:
        return (
            f"Futures Plan: {entry_label} {_fmt_price(signal.entry)} | "
            f"Hedef {_fmt_price(signal.target)} | Stop {_fmt_price(signal.stop)} | R/R {rr_text}"
        )

    risk = signal.risk_plan
    base_asset = _base_asset(symbol)
    return "\n".join(
        [
            "Futures Risk Plan:",
            f"Balance: {risk.account_balance:.0f} USDT",
            f"Confidence: {signal.confidence}/100",
            f"Risk: {risk.risk_pct:.1f}% ({risk.risk_amount:.2f} USDT)",
            (
                "Daily guard: "
                f"loss {risk.daily_loss_pct:.1f}/{risk.max_daily_loss_pct:.1f}% | "
                f"loss trades {risk.daily_losing_trades}/{risk.max_daily_losing_trades}"
            ),
            f"Entry: {_fmt_price(signal.entry)}" if not signal.needs_trigger else f"Entry/Tetik: {_fmt_price(signal.entry)}",
            f"Stop Loss: {_fmt_price(signal.stop)}",
            f"TP1: {_fmt_price(risk.tp1)}",
            f"TP2: {_fmt_price(risk.tp2)}",
            f"R/R: {rr_text}",
            f"Position size: {_fmt_quantity(risk.position_size)} {base_asset}",
            f"Suggested margin: {risk.suggested_margin:.2f} USDT",
            f"Suggested leverage: {risk.suggested_leverage}x",
            f"Risk status: {risk.status.value}" + (f" | {risk.reason}" if risk.reason else ""),
        ]
    )


def _market_regime_line(regime: MarketRegimeResult) -> str:
    emoji = {
        MarketRegime.BULL: "🟢",
        MarketRegime.BEAR: "🔴",
        MarketRegime.RANGE: "🟡",
        MarketRegime.LATE_BULL: "🟠",
    }[regime.regime]
    dominance = (
        f" | BTC.D {regime.btc_dominance_pct:.1f}%"
        if regime.btc_dominance_pct is not None
        else ""
    )
    warning = f" | {regime.warning}" if regime.warning else ""
    rr_policy = " | R/R min 1:2.5" if regime.regime == MarketRegime.RANGE else ""
    return (
        f"Piyasa Rejimi: {regime.regime.value} {emoji} | "
        f"Güven {regime.confidence}/100 | {regime.reason}{dominance} | "
        f"{regime.leverage_note}{rr_policy}{warning}"
    )


def _futures_market_mode_line(prepared: list[dict[str, object]]) -> str:
    signals = [
        signal
        for item in prepared
        if isinstance((signal := item.get("futures_signal")), FuturesSignal)
    ]
    if not signals:
        return "PİYASA MODU: BEKLE 🟡 | Güven 0/100 🔴"

    active = [signal for signal in signals if signal.side in {"LONG", "SHORT"}]
    if active:
        strongest = max(active, key=lambda signal: signal.confidence)
        active_same_side = sum(1 for signal in active if signal.side == strongest.side)
        suffix = f" | aktif {active_same_side}" if len(signals) > 1 else ""
        return (
            f"PİYASA MODU: {strongest.side} AĞIRLIKLI {_futures_side_emoji(strongest.side)} | "
            f"Güven {strongest.confidence}/100 {_score_emoji(strongest.confidence)}{suffix}"
        )

    strongest = max(signals, key=lambda signal: signal.confidence)
    if strongest.bias in {"LONG", "SHORT"}:
        return (
            f"PİYASA MODU: BEKLE 🟡 | eğilim {strongest.bias} | "
            f"Güven {strongest.confidence}/100 {_score_emoji(strongest.confidence)}"
        )
    return f"PİYASA MODU: BEKLE 🟡 | Güven {strongest.confidence}/100 {_score_emoji(strongest.confidence)}"


def _btc_4h_bearish(analyses: list[SymbolAnalysis]) -> bool:
    for symbol_analysis in analyses:
        if symbol_analysis.symbol != "BTCUSDT":
            continue
        for item in symbol_analysis.timeframes:
            if item.timeframe == "4h":
                return item.trend == "bearish" or item.trend_score <= -4
    return False


def _crash_warning(
    analyses: list[SymbolAnalysis],
    flow_stats: dict[str, MarketStat] | None,
    confirm_stats: dict[str, MarketStat] | None,
) -> str | None:
    btc = next((analysis for analysis in analyses if analysis.symbol == "BTCUSDT"), None)
    if not btc:
        return "Çöküş riski: BTC verisi yok, temkinli ol ⚠️"

    frames = _timeframe_map(btc)
    item_15m = frames.get("15m")
    item_1h = frames.get("1h")
    item_4h = frames.get("4h")
    flow = flow_stats.get("BTCUSDT") if flow_stats else None
    confirm = confirm_stats.get("BTCUSDT") if confirm_stats else None
    if not item_15m or not item_1h or not item_4h:
        return None

    score = 0
    reasons: list[str] = []
    support_broken = item_4h.close <= item_4h.support or item_1h.close <= item_1h.support
    near_support = (
        not support_broken
        and (
            item_4h.close <= item_4h.support * 1.02
            or item_1h.close <= item_1h.support * 1.01
        )
    )

    if item_15m.trend_score <= -3 or item_15m.close < item_15m.ema20:
        score += 1
        reasons.append("BTC kısa vade zayif")
    if item_1h.trend_score <= -3 or item_1h.close < item_1h.ema20:
        score += 1
        reasons.append("BTC trend zayif")
    if item_4h.trend_score <= -4:
        score += 1
        reasons.append("BTC ana trend zayif")
    if support_broken:
        score += 2
        reasons.append("destek kirildi")
    elif near_support:
        score += 1
        reasons.append("destek yakin")
    if flow and flow.price_change_pct <= -0.20:
        score += 1
        reasons.append(f"5M para cikisi {flow.price_change_pct:+.1f}%")
    if confirm and confirm.price_change_pct <= -0.50:
        score += 1
        reasons.append(f"teyit para cikisi {confirm.price_change_pct:+.1f}%")
    if item_15m.volume_change_pct >= 80 and item_15m.change_pct < 0:
        score += 1
        reasons.append("hacimli satis")
    if item_15m.close <= item_15m.support * 1.005:
        score += 1
        reasons.append("kisa destek dibinde")

    if score >= 4 and support_broken:
        return "Çöküş riski YÜKSEK ⚠️ " + " | ".join(reasons[:3])
    if score >= 4:
        return "Destek kırılmadı, risk artıyor ⚠️ " + " | ".join(reasons[:3])
    if score >= 3:
        return "Zayıflama uyarısı ⚠️ " + " | ".join(reasons[:3])
    return None


def _flow_summary(
    analyses: list[SymbolAnalysis],
    sectors: dict[str, str] | None,
    flow_stats: dict[str, MarketStat] | None,
    confirm_stats: dict[str, MarketStat] | None = None,
    market_stats: dict[str, MarketStat] | None = None,
) -> str | None:
    if not sectors:
        return None

    instant_amounts, instant_symbols = _sector_flow_amounts(flow_stats, sectors)
    confirm_amounts, confirm_symbols = _sector_flow_amounts(confirm_stats, sectors)
    daily_amounts, daily_symbols = _sector_flow_amounts(market_stats, sectors)

    if not instant_amounts and not confirm_amounts and not daily_amounts:
        return None

    if instant_amounts:
        positive_total = sum(amount for amount in instant_amounts.values() if amount > 0)
        negative_total = sum(-amount for amount in instant_amounts.values() if amount < 0)
        total = positive_total - negative_total
        totals_suffix = _flow_totals_suffix(flow_stats)
        leader, leader_amount = max(instant_amounts.items(), key=lambda item: item[1])
        leader_symbol = _top_flow_symbol(instant_symbols.get(leader, []))

        if negative_total > positive_total * 1.25 and total <= -25_000:
            return f"Anlık Net Akış: USDT'ye kaçış {_fmt_amount(total, '$')} | {_flow_strength(total)}{totals_suffix}"

        if leader_amount > 0:
            suffix = f" ({leader_symbol})" if leader_symbol else ""
            if abs(leader_amount) < 25_000:
                return f"Anlık Net Akış: net zayıf | {leader} {_fmt_amount(leader_amount, '$')} çok zayıf{totals_suffix}"
            return f"Anlık Net Akış: {leader} {_fmt_amount(leader_amount, '$')} | {_flow_strength(leader_amount)}{suffix}{totals_suffix}"

        if total < 0:
            return f"Anlık Net Akış: satış baskısı {_fmt_amount(total, '$')} | {_flow_strength(total)}{totals_suffix}"

    if confirm_amounts:
        confirm_leader, confirm_amount = max(confirm_amounts.items(), key=lambda item: item[1])
        if confirm_amount > 50_000:
            symbol = _top_flow_symbol(confirm_symbols.get(confirm_leader, []))
            suffix = f" ({symbol})" if symbol else ""
            return f"Teyit Akışı: anlık zayıf, para {confirm_leader} içinde {_fmt_amount(confirm_amount, '$')}{suffix}"

    if daily_amounts:
        daily_leader, daily_amount = max(daily_amounts.items(), key=lambda item: item[1])
        if daily_amount > 100_000:
            symbol = _top_flow_symbol(daily_symbols.get(daily_leader, []))
            suffix = f" ({symbol})" if symbol else ""
            return f"24s Akış: yeni giriş zayıf, eski para {daily_leader} tarafında{suffix}"

    return "Akış: Net güçlü sektör yok"


def _correction_rotation(
    analyses: list[SymbolAnalysis],
    sectors: dict[str, str] | None,
    flow_stats: dict[str, MarketStat] | None,
    confirm_stats: dict[str, MarketStat] | None,
) -> str | None:
    if not flow_stats:
        return None

    tracked = [analysis.symbol for analysis in analyses if analysis.symbol != "BTCUSDT"]
    if not tracked:
        return None

    btc_5m = flow_stats.get("BTCUSDT")
    btc_1h = confirm_stats.get("BTCUSDT") if confirm_stats else None
    btc_soft = bool(
        (btc_5m and btc_5m.price_change_pct <= -0.10)
        or (btc_1h and btc_1h.price_change_pct <= -0.25)
    )

    instant_amounts, instant_symbols = _sector_flow_amounts(flow_stats, sectors)
    positive_total = sum(amount for amount in instant_amounts.values() if amount > 0)
    negative_total = sum(-amount for amount in instant_amounts.values() if amount < 0)
    total_flow = positive_total - negative_total
    negative_symbols = []
    for symbol in tracked:
        stat = flow_stats.get(symbol)
        amount = _flow_pressure_usd(stat) if stat else None
        if amount is not None and (amount < -25_000 or stat.price_change_pct < 0):
            negative_symbols.append(symbol)

    if not btc_soft and len(negative_symbols) < max(2, len(tracked) // 2):
        return None

    negative_ratio = len(negative_symbols) / len(tracked)
    if negative_total > positive_total * 1.25 and total_flow <= -25_000:
        positive_sectors = {
            sector: amount
            for sector, amount in instant_amounts.items()
            if amount > 25_000
        }
        if negative_ratio >= 0.70 or not positive_sectors:
            return "Düzeltme: Para coinlerden çıkıp USDT tarafında bekliyor"
        leader, _ = max(positive_sectors.items(), key=lambda item: item[1])
        names = _top_flow_names(instant_symbols.get(leader, []))
        suffix = f" ({names})" if names else ""
        return f"Düzeltme: Net çıkış var; en güçlü {leader}{suffix}"

    if instant_amounts:
        positive_sectors = {
            sector: amount
            for sector, amount in instant_amounts.items()
            if amount > max(50_000, negative_total * 0.8)
        }
        if positive_sectors:
            leader, _ = max(positive_sectors.items(), key=lambda item: item[1])
            names = _top_flow_names(instant_symbols.get(leader, []))
            suffix = f" ({names})" if names else ""
            return f"Düzeltme: Para {leader} tarafına kayıyor{suffix}"

    if confirm_stats:
        confirm_positive = [
            symbol
            for symbol in tracked
            if (stat := confirm_stats.get(symbol)) and stat.price_change_pct > 0
        ]
        if sectors and confirm_positive:
            sector_counts: dict[str, int] = {}
            for symbol in confirm_positive:
                sector = sectors.get(symbol)
                if sector:
                    sector_counts[sector] = sector_counts.get(sector, 0) + 1
            if sector_counts:
                leader, _ = max(sector_counts.items(), key=lambda item: item[1])
                return f"Düzeltme: Yeni giriş zayıf, para {leader} içinde dönüyor"

    return None


def build_report(
    analyses: list[SymbolAnalysis],
    costs: dict[str, float] | None = None,
    sectors: dict[str, str] | None = None,
    btc_unavailable: bool = False,
    market_stats: dict[str, MarketStat] | None = None,
    flow_stats: dict[str, MarketStat] | None = None,
    confirm_stats: dict[str, MarketStat] | None = None,
    order_books: dict[str, OrderBookPressure] | None = None,
    onchain_flows: dict[str, OnChainFlow] | None = None,
    stablecoin_reserve: StablecoinReserve | None = None,
    exchange_consensus: dict[str, ExchangeConsensus] | None = None,
    market_context: MarketContext | None = None,
    futures_flows: dict[str, FuturesFlowSnapshot] | None = None,
) -> str:
    report_time = _report_time_line()
    btc_4h_bearish = _btc_4h_bearish(analyses)
    crash_text = _crash_warning(analyses, flow_stats, confirm_stats)
    prepared: list[dict[str, object]] = []
    candidates: list[SignalCandidate] = []
    futures_candidates: list[FuturesSignalCandidate] = []
    confidence_scores: list[int] = []
    news_impact = _news_impact(market_context)
    performance_profile = load_signal_performance_profile()
    optimized_weights = load_signal_weights()
    min_entry_confidence = optimized_weights.min_entry_confidence or MIN_ENTRY_CONFIDENCE
    min_entry_rr = optimized_weights.min_entry_rr or MIN_ENTRY_RR
    futures_mode = FUTURES_MODE_ENABLED
    market_regime = _market_regime_from_analyses(analyses, market_context) if futures_mode else None
    history_daily_loss_pct, history_daily_losing_trades = (
        daily_futures_risk_state() if futures_mode else (0.0, 0)
    )
    futures_daily_loss_pct = max(FUTURES_TODAY_LOSS_PCT, history_daily_loss_pct)
    futures_daily_losing_trades = max(
        FUTURES_TODAY_LOSING_TRADES,
        history_daily_losing_trades,
    )

    for symbol_analysis in analyses:
        cost = _cost_for(symbol_analysis.symbol, costs)
        last_item = symbol_analysis.timeframes[-1]
        day_levels = _day_trade_levels(symbol_analysis)
        display_close = day_levels[0] if day_levels else last_item.close
        altcoin_blocked = (btc_4h_bearish or btc_unavailable) and symbol_analysis.symbol != "BTCUSDT"
        order_book = order_books.get(symbol_analysis.symbol) if order_books else None
        futures_flow = futures_flows.get(symbol_analysis.symbol) if futures_flows else None
        onchain_flow = onchain_flows.get(symbol_analysis.symbol) if onchain_flows else None
        exchange_view = exchange_consensus.get(symbol_analysis.symbol) if exchange_consensus else None
        alarm_lines = [
            alarm
            for item in symbol_analysis.timeframes
            if (alarm := _alarm_short(item, cost))
        ]
        entry_line, entry_allowed = _entry_decision(
            symbol_analysis,
            altcoin_blocked,
            order_book,
            flow_stats,
            onchain_flow,
            stablecoin_reserve,
        )
        if entry_allowed and news_impact <= -12:
            entry_line = "Giriş: BEKLE 🟡 | haber riski"
            entry_allowed = False

        setup = _trade_setup_values(symbol_analysis, entry_allowed)
        rr = _risk_reward_ratio(setup)
        if entry_allowed and rr is not None and rr < 1:
            entry_line = "Giriş: HAYIR 🔴 | R/R düşük"
            entry_allowed = False
            setup = _trade_setup_values(symbol_analysis, entry_allowed)
            rr = _risk_reward_ratio(setup)

        confidence = _confidence_score(
            symbol_analysis,
            entry_allowed,
            altcoin_blocked,
            order_book,
            flow_stats,
            confirm_stats,
            onchain_flow,
            stablecoin_reserve,
            market_context,
            rr,
            optimized_weights,
            market_regime,
        )
        quality_adjust, quality_line = _quality_adjustment(
            symbol_analysis.symbol,
            market_stats,
            flow_stats,
            order_book,
        )
        performance_adjust = performance_profile.adjustment_for(symbol_analysis.symbol)
        performance_note = performance_profile.note_for(symbol_analysis.symbol)
        optimized_adjust = optimized_weights.adjustment_for(symbol_analysis.symbol)
        optimized_note = optimized_weights.note_for(symbol_analysis.symbol)
        confidence = _clamp(confidence + quality_adjust + performance_adjust + optimized_adjust)

        if entry_allowed and quality_adjust <= -8:
            entry_line = "Giriş: BEKLE 🟡 | kalite filtresi"
            entry_allowed = False
            setup = _trade_setup_values(symbol_analysis, entry_allowed)
            rr = _risk_reward_ratio(setup)
            confidence = _confidence_score(
                symbol_analysis,
                entry_allowed,
                altcoin_blocked,
                order_book,
                flow_stats,
                confirm_stats,
                onchain_flow,
                stablecoin_reserve,
                market_context,
                rr,
                optimized_weights,
                market_regime,
            )
            confidence = _clamp(confidence + quality_adjust + performance_adjust + optimized_adjust)
        if entry_allowed:
            quality_reasons: list[str] = []
            if confidence < min_entry_confidence:
                quality_reasons.append("güven düşük")
            if rr is None or rr < min_entry_rr:
                quality_reasons.append("R/R düşük")
            if quality_reasons:
                entry_line = f"Giriş: BEKLE 🟡 | {', '.join(quality_reasons)}"
                entry_allowed = False
                setup = _trade_setup_values(symbol_analysis, entry_allowed)
                rr = _risk_reward_ratio(setup)
                confidence = _confidence_score(
                    symbol_analysis,
                    entry_allowed,
                    altcoin_blocked,
                    order_book,
                    flow_stats,
                    confirm_stats,
                    onchain_flow,
                    stablecoin_reserve,
                    market_context,
                    rr,
                    optimized_weights,
                    market_regime,
                )
                confidence = _clamp(confidence + quality_adjust + performance_adjust + optimized_adjust)
        confidence_scores.append(confidence)

        futures_signal = (
            _futures_signal(
                symbol_analysis,
                altcoin_blocked,
                order_book,
                flow_stats,
                confirm_stats,
                onchain_flow,
                stablecoin_reserve,
                market_context,
                market_regime,
                futures_daily_loss_pct,
                futures_daily_losing_trades,
                futures_flow,
                optimized_weights,
            )
            if futures_mode
            else None
        )

        frames = _timeframe_map(symbol_analysis)
        item_15m = frames.get("15m")
        risk_skipped = bool(
            isinstance(futures_signal, FuturesSignal)
            and futures_signal.risk_plan
            and futures_signal.risk_plan.status in {RiskStatus.SKIP, RiskStatus.NO_TRADE}
        )
        if futures_signal and futures_signal.entry and futures_signal.target and futures_signal.stop and not risk_skipped:
            futures_candidates.append(
                FuturesSignalCandidate(
                    symbol=symbol_analysis.symbol,
                    side=futures_signal.side,
                    bias=futures_signal.bias,
                    entry=futures_signal.entry,
                    target=futures_signal.target,
                    stop=futures_signal.stop,
                    current_price=display_close,
                    confidence=futures_signal.confidence,
                    rr=futures_signal.rr or 0.0,
                    decision=_futures_decision_line(futures_signal),
                    needs_trigger=futures_signal.needs_trigger,
                    risk_pct=(
                        futures_signal.risk_plan.risk_pct
                        if futures_signal.risk_plan
                        else 0.0
                    ),
                )
            )
        if not futures_mode and setup and item_15m:
            entry, target, stop, _, needs_trigger = setup
            candidates.append(
                SignalCandidate(
                    symbol=symbol_analysis.symbol,
                    entry=entry,
                    target=target,
                    stop=stop,
                    current_price=display_close,
                    recent_high=item_15m.recent_high,
                    recent_low=item_15m.recent_low,
                    confidence=confidence,
                    rr=rr or 0.0,
                    active=entry_allowed and confidence >= min_entry_confidence and (rr or 0.0) >= min_entry_rr,
                    decision=entry_line,
                    needs_trigger=needs_trigger,
                )
            )

        prepared.append(
            {
                "symbol_analysis": symbol_analysis,
                "display_close": display_close,
                "entry_line": entry_line,
                "entry_allowed": entry_allowed,
                "altcoin_blocked": altcoin_blocked,
                "order_book": order_book,
                "onchain_flow": onchain_flow,
                "exchange_view": exchange_view,
                "alarm_lines": alarm_lines,
                "confidence": confidence,
                "rr": rr,
                "setup": setup,
                "futures_signal": futures_signal,
                "quality_line": quality_line,
                "performance_note": performance_note,
                "optimized_note": optimized_note,
            }
        )

    signal_result = track_futures_signals(futures_candidates) if futures_mode else track_signals(candidates)
    profile = "/".join(analysis.symbol.replace("USDT", "") for analysis in analyses)
    lines = [
        "KRIPTO RAPORU",
        report_time,
        f"Profil: {profile}",
        f"Sürüm: {REPORT_PROFILE_VERSION}",
        *(["Hesap: FUTURES | yön modu LONG/SHORT/BEKLE"] if futures_mode else []),
        *([_market_regime_line(market_regime)] if futures_mode and market_regime else []),
        (
            _futures_market_mode_line(prepared)
            if futures_mode
            else _market_mode_line(crash_text, btc_4h_bearish, btc_unavailable, confidence_scores, market_context)
        ),
        signal_result.summary_line,
        signal_result.audit_line,
    ]

    macro_parts = []
    if btc_line := _btc_dominance_line(market_context):
        macro_parts.append(btc_line)
    macro_parts.append(_news_filter_line(market_context))
    lines.append(" | ".join(macro_parts))

    if flow_text := _simple_total_flow_line(flow_stats):
        lines.append(flow_text)
    if exchange_line := _exchange_consensus_summary(exchange_consensus):
        lines.append(exchange_line)
    if stable_line := _stablecoin_line(stablecoin_reserve):
        lines.append(stable_line)
    if flow_summary := _flow_summary(analyses, sectors, flow_stats, confirm_stats, market_stats):
        lines.append(flow_summary)
    if rotation := _correction_rotation(analyses, sectors, flow_stats, confirm_stats):
        lines.append(rotation)
    if crash_text:
        lines.append(f"Risk: {crash_text}")
    if performance_profile.summary_line:
        lines.append(performance_profile.summary_line)

    for item in prepared:
        symbol_analysis = item["symbol_analysis"]
        if not isinstance(symbol_analysis, SymbolAnalysis):
            continue
        display_close = float(item["display_close"])
        entry_line = str(item["entry_line"])
        entry_allowed = bool(item["entry_allowed"])
        altcoin_blocked = bool(item["altcoin_blocked"])
        order_book = item["order_book"]
        onchain_flow = item["onchain_flow"]
        exchange_view = item["exchange_view"]
        alarm_lines = item["alarm_lines"]
        confidence = int(item["confidence"])
        rr = item["rr"] if isinstance(item["rr"], float) or item["rr"] is None else None
        setup = item["setup"]
        futures_signal = item["futures_signal"]
        quality_line = item["quality_line"]
        performance_note = item["performance_note"]
        optimized_note = item["optimized_note"]
        futures_plan = (
            _futures_plan_line(symbol_analysis.symbol, futures_signal)
            if isinstance(futures_signal, FuturesSignal)
            else None
        )
        futures_flow = (
            _futures_flow_line(futures_signal.futures_flow)
            if isinstance(futures_signal, FuturesSignal)
            else None
        )

        lines.extend([
            "",
            f"{symbol_analysis.symbol} | Fiyat: {_fmt_price(display_close)}",
            *(
                [_futures_decision_line(futures_signal)]
                if isinstance(futures_signal, FuturesSignal)
                else [_simple_decision_line(entry_line)]
            ),
            *([futures_flow] if futures_flow else []),
            *([futures_plan] if futures_plan else []),
            *([] if futures_mode else [_confidence_line(confidence, rr)]),
            *([flow_line] if (flow_line := _simple_symbol_flow(symbol_analysis.symbol, flow_stats)) else []),
            *(
                [exchange_line]
                if (
                    exchange_line := _exchange_consensus_line(
                        exchange_view if isinstance(exchange_view, ExchangeConsensus) else None
                    )
                )
                else []
            ),
            *(
                [chain_line]
                if (
                    chain_line := _onchain_line(
                        onchain_flow if isinstance(onchain_flow, OnChainFlow) else None
                    )
                )
                else []
            ),
            *([str(quality_line)] if not futures_mode and isinstance(quality_line, str) and confidence >= 45 else []),
            *([str(performance_note)] if not futures_mode and _show_learning_note(performance_note, confidence) else []),
            *([str(optimized_note)] if not futures_mode and _show_learning_note(optimized_note, confidence) else []),
            *([big_order] if (big_order := _big_order_line(order_book if isinstance(order_book, OrderBookPressure) else None)) else []),
            *(
                [guard_line]
                if (
                    not futures_mode
                    and (guard_line := _profit_guard_line(
                        symbol_analysis,
                        flow_stats,
                        confirm_stats,
                        order_book if isinstance(order_book, OrderBookPressure) else None,
                    ))
                )
                else []
            ),
            *(
                [fake_line]
                if (
                    fake_line := _fake_pump_line(
                        symbol_analysis,
                        order_book if isinstance(order_book, OrderBookPressure) else None,
                        onchain_flow if isinstance(onchain_flow, OnChainFlow) else None,
                    )
                )
                else []
            ),
            *(
                [dip_reversal]
                if (
                    not futures_mode
                    and (dip_reversal := _dip_reversal_line(
                        symbol_analysis,
                        flow_stats,
                        confirm_stats,
                        order_book if isinstance(order_book, OrderBookPressure) else None,
                        altcoin_blocked,
                    ))
                )
                else []
            ),
            *(
                [rocket]
                if (
                    not futures_mode
                    and (rocket := _rocket_line(
                        symbol_analysis,
                        flow_stats,
                        confirm_stats,
                        order_book if isinstance(order_book, OrderBookPressure) else None,
                        entry_allowed,
                        altcoin_blocked,
                    ))
                )
                else []
            ),
            *(
                [strong_rise]
                if (
                    not futures_mode
                    and (strong_rise := _strong_rise_alert_line(
                        symbol_analysis,
                        flow_stats,
                        confirm_stats,
                        order_book if isinstance(order_book, OrderBookPressure) else None,
                        entry_allowed,
                        altcoin_blocked,
                    ))
                )
                else []
            ),
            *(
                [rise]
                if (
                    not futures_mode
                    and (rise := _rise_signal(
                        symbol_analysis,
                        flow_stats,
                        confirm_stats,
                        altcoin_blocked,
                    ))
                )
                else []
            ),
            *([] if futures_mode else _simple_trade_lines(symbol_analysis, entry_allowed, entry_line)),
            *(
                [real_check]
                if (
                    not futures_mode
                    and
                    not _is_no_trade_entry(entry_line)
                    and (real_check := _real_trade_check_line(symbol_analysis, entry_allowed))
                )
                else []
            ),
            *([test_line] if (test_line := signal_result.symbol_lines.get(symbol_analysis.symbol)) else []),
            *(
                [protection]
                if (
                    not futures_mode
                    and
                    entry_allowed
                    and (protection := _protection_line(
                        setup if isinstance(setup, tuple) else None,
                        display_close,
                        entry_allowed,
                    ))
                )
                else []
            ),
            *(
                [_simple_alarm_line(alarm_lines)]
                if isinstance(alarm_lines, list) and alarm_lines
                else []
            ),
        ])
    return "\n".join(lines)
