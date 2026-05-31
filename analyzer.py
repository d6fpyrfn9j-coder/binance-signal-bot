"""Analysis logic for symbols and timeframes."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from data_fetcher import Candle
from data_fetcher import MarketStat, OrderBookPressure
from indicators import bollinger_bands, ema, macd, momentum_pct, previous_ema_pair, rsi, sma


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
    candle_pattern: str | None
    warnings: list[str]


@dataclass(frozen=True)
class SymbolAnalysis:
    symbol: str
    timeframes: list[TimeframeAnalysis]


def _trend_from_score(score: int) -> str:
    if score >= 5:
        return "bullish"
    if score <= -5:
        return "bearish"
    return "neutral"


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

    last_rsi = rsi(closes)
    ema7_now = ema(closes, 7)
    ema20_now = ema(closes, 20)
    ema50_now = ema(closes, 50)
    ema200_now = ema(closes, 200)
    ema20_prev, ema50_prev = previous_ema_pair(closes, 20, 50)
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
    if value >= 100:
        return f"{value:.0f}"
    return f"{value:.2f}"


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
    return f"{_timeframe_label(item.timeframe)} {first_line}"


def _decision(item: TimeframeAnalysis, cost: float | None, altcoin_blocked: bool = False) -> str:
    if altcoin_blocked:
        return "BTC 4H ZAYIF - BEKLE"
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


def _fake_risk_score(item: TimeframeAnalysis, order_book: OrderBookPressure | None) -> int:
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
    return score


def _footprint_line(symbol_analysis: SymbolAnalysis, order_book: OrderBookPressure | None) -> str | None:
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

    if _fake_risk_score(item, order_book) >= 3:
        signals.append("Fake YÜKSEK ⚠️")

    return "İz: " + " | ".join([side, *signals])


def _position_line(
    symbol_analysis: SymbolAnalysis,
    altcoin_blocked: bool,
    order_book: OrderBookPressure | None,
) -> str | None:
    frames = _timeframe_map(symbol_analysis)
    item_15m = frames.get("15m")
    item_1h = frames.get("1h")
    if not item_15m or not item_1h:
        return None

    fake_score = max(_fake_risk_score(item_15m, order_book), _fake_risk_score(item_1h, order_book))
    orderbook_weak = bool(order_book and order_book.imbalance_pct <= -20)

    if altcoin_blocked:
        return "Pozisyon: ZARAR RİSKİ 🔴 | BTC zayıf"
    if fake_score >= 3:
        return "Pozisyon: FAKE RİSKİ ⚠️ | kar güveni düşük"
    if item_1h.trend_score <= -5 or (item_15m.trend_score <= -5 and item_1h.trend_score <= -3):
        return "Pozisyon: ZARAR RİSKİ 🔴 | trend düşüşte"
    if item_15m.taker_delta_pct <= -15 and item_1h.trend_score <= 0:
        return "Pozisyon: ZARAR RİSKİ 🔴 | satıcı baskısı"
    if (
        item_1h.trend_score >= 6
        and item_15m.trend_score >= 3
        and item_15m.taker_delta_pct >= 0
        and not orderbook_weak
    ):
        return "Pozisyon: KÂR BEKLENTİSİ 🟢 | 1H yüksek"
    if item_1h.trend_score >= 3 and item_15m.trend_score >= 0 and item_15m.taker_delta_pct >= -5:
        return "Pozisyon: KÂR BEKLENTİSİ 🟢 | 1H toparlıyor"
    return "Pozisyon: BELİRSİZ 🟡 | izle"


def _timeframe_map(symbol_analysis: SymbolAnalysis) -> dict[str, TimeframeAnalysis]:
    return {item.timeframe: item for item in symbol_analysis.timeframes}


def _rise_signal(
    symbol_analysis: SymbolAnalysis,
    flow_stats: dict[str, MarketStat] | None,
    confirm_stats: dict[str, MarketStat] | None,
    altcoin_blocked: bool,
) -> str | None:
    if altcoin_blocked:
        return None

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

    if (item_15m.fake_rise_risk or item_15m.distribution_risk) and flow.price_change_pct > 0:
        return "Yükseliş fake olabilir ⚠️"
    if flow_positive and rsi_ok and item_15m.trend_score >= 3 and item_1h.trend_score >= 3:
        return "Trend erken güçleniyor 🟢"
    if flow_positive and rsi_ok and (confirm_positive or trend_ok or near_breakout):
        return "Yükseliş sinyali var 🟢"
    if near_breakout and momentum_ok and rsi_ok:
        return "Kırılım yaklaşıyor 🟢"
    return None


def _trade_zones(item: TimeframeAnalysis, altcoin_blocked: bool) -> str:
    if altcoin_blocked:
        return "Plan: BTC zayif, yeni giris bekle"

    support = item.support
    resistance = item.resistance
    close = item.close
    entry_low = support
    entry_high = support * 1.01
    risk = support * 0.985

    if close >= resistance:
        target = resistance + max((resistance - support) * 0.5, close * 0.015)
        return f"Plan: Kırılım üstü izle | Hedef {_fmt_price(target)} | Risk {_fmt_price(resistance)} altı"
    if close <= support:
        return f"Plan: Destek kırıldı | Risk {_fmt_price(risk)} altı | Giriş bekle"
    return (
        f"Plan: Giriş {_fmt_price(entry_low)}-{_fmt_price(entry_high)} | "
        f"Hedef {_fmt_price(resistance)} | Risk {_fmt_price(risk)} altı"
    )


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

    if item_15m.trend_score <= -3 or item_15m.close < item_15m.ema20:
        score += 1
        reasons.append("BTC 15M zayif")
    if item_1h.trend_score <= -3 or item_1h.close < item_1h.ema20:
        score += 1
        reasons.append("BTC 1H zayif")
    if item_4h.trend_score <= -4 or item_4h.close <= item_4h.support * 1.02:
        score += 1
        reasons.append("BTC 4H riskli")
    if flow and flow.price_change_pct <= -0.20:
        score += 1
        reasons.append(f"5M para cikisi {flow.price_change_pct:+.1f}%")
    if confirm and confirm.price_change_pct <= -0.50:
        score += 1
        reasons.append(f"1H para cikisi {confirm.price_change_pct:+.1f}%")
    if item_15m.volume_change_pct >= 80 and item_15m.change_pct < 0:
        score += 1
        reasons.append("hacimli satis")
    if item_15m.close <= item_15m.support * 1.005:
        score += 1
        reasons.append("15M destek dibinde")

    if score >= 4:
        return "Çöküş riski YÜKSEK ⚠️ " + " | ".join(reasons[:3])
    if score >= 3:
        return "Çöküş erken uyarı ⚠️ " + " | ".join(reasons[:3])
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

    instant_scores: dict[str, float] = {}
    confirm_scores: dict[str, float] = {}
    daily_scores: dict[str, float] = {}
    for symbol_analysis in analyses:
        symbol = symbol_analysis.symbol
        if symbol == "BTCUSDT":
            continue
        sector = sectors.get(symbol)
        if not sector:
            continue
        if flow_stats and (stat := flow_stats.get(symbol)):
            score = stat.quote_volume * max(stat.price_change_pct, -5)
            instant_scores[sector] = instant_scores.get(sector, 0.0) + score
        if confirm_stats and (stat := confirm_stats.get(symbol)):
            score = stat.quote_volume * max(stat.price_change_pct, -5)
            confirm_scores[sector] = confirm_scores.get(sector, 0.0) + score
        if market_stats and (stat := market_stats.get(symbol)):
            score = stat.quote_volume * max(stat.price_change_pct, -5)
            daily_scores[sector] = daily_scores.get(sector, 0.0) + score

    if not instant_scores and not confirm_scores and not daily_scores:
        return None

    if instant_scores:
        instant_leader, instant_score = max(instant_scores.items(), key=lambda item: item[1])
        if instant_score > 0:
            return f"Anlık Akış: {instant_leader} önde"

    if confirm_scores:
        confirm_leader, confirm_score = max(confirm_scores.items(), key=lambda item: item[1])
        if confirm_score > 0:
            return f"Akış: Anlık net değil, para {confirm_leader} içinde dönüyor"

    if daily_scores:
        daily_leader, daily_score = max(daily_scores.items(), key=lambda item: item[1])
        if daily_score > 0:
            return f"Akış: Yeni giriş zayıf, önceki para {daily_leader} tarafında"

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

    negative_symbols = [
        symbol
        for symbol in tracked
        if (stat := flow_stats.get(symbol)) and stat.price_change_pct < 0
    ]
    positive_symbols = [
        symbol
        for symbol in tracked
        if (stat := flow_stats.get(symbol)) and stat.price_change_pct > 0
    ]

    if not btc_soft and len(negative_symbols) < max(2, len(tracked) // 2):
        return None

    if sectors and positive_symbols:
        sector_scores: dict[str, float] = {}
        sector_symbols: dict[str, list[str]] = {}
        for symbol in positive_symbols:
            sector = sectors.get(symbol)
            stat = flow_stats.get(symbol)
            if not sector or not stat:
                continue
            sector_scores[sector] = sector_scores.get(sector, 0.0) + stat.quote_volume * stat.price_change_pct
            sector_symbols.setdefault(sector, []).append(symbol.replace("USDT", ""))
        if sector_scores:
            leader, score = max(sector_scores.items(), key=lambda item: item[1])
            if score > 0:
                names = ", ".join(sector_symbols.get(leader, [])[:3])
                return f"Düzeltme: Para {leader} tarafına kayıyor ({names})"

    negative_ratio = len(negative_symbols) / len(tracked)
    if negative_ratio >= 0.70:
        return "Düzeltme: Para coinlerden çıkıp USDT tarafında bekliyor"

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
) -> str:
    report_time = dt.datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
    btc_4h_bearish = _btc_4h_bearish(analyses)
    lines = [
        "KRIPTO RAPORU",
        report_time,
    ]
    if btc_4h_bearish:
        lines.append("BTC 4H zayif: altcoin bekle modu aktif")
    elif btc_unavailable:
        lines.append("BTC verisi yok: altcoin bekle modu aktif")
    if crash_text := _crash_warning(analyses, flow_stats, confirm_stats):
        lines.append(crash_text)
    if flow_text := _flow_summary(analyses, sectors, flow_stats, confirm_stats, market_stats):
        lines.append(flow_text)
    if rotation_text := _correction_rotation(analyses, sectors, flow_stats, confirm_stats):
        lines.append(rotation_text)

    for symbol_analysis in analyses:
        cost = _cost_for(symbol_analysis.symbol, costs)
        last_item = symbol_analysis.timeframes[-1]
        sector = sectors.get(symbol_analysis.symbol) if sectors else None
        altcoin_blocked = (btc_4h_bearish or btc_unavailable) and symbol_analysis.symbol != "BTCUSDT"
        order_book = order_books.get(symbol_analysis.symbol) if order_books else None
        alarm_lines = [
            alarm
            for item in symbol_analysis.timeframes
            if (alarm := _alarm_short(item, cost))
        ]
        rise_signal = _rise_signal(symbol_analysis, flow_stats, confirm_stats, altcoin_blocked)

        lines.extend([
            "",
            symbol_analysis.symbol,
            *([f"Sektor: {sector}"] if sector else []),
            f"Fiyat: {_fmt_price(last_item.close)}",
            *([position] if (position := _position_line(symbol_analysis, altcoin_blocked, order_book)) else []),
            *([footprint] if (footprint := _footprint_line(symbol_analysis, order_book)) else []),
            *([rise_signal] if rise_signal else []),
            *(
                [
                    f"5M: {flow_stats[symbol_analysis.symbol].price_change_pct:+.1f}% | "
                    f"1H: {confirm_stats[symbol_analysis.symbol].price_change_pct:+.1f}%"
                ]
                if flow_stats and confirm_stats and symbol_analysis.symbol in flow_stats and symbol_analysis.symbol in confirm_stats
                else []
            ),
            *(
                [f"5M: {flow_stats[symbol_analysis.symbol].price_change_pct:+.1f}%"]
                if flow_stats and symbol_analysis.symbol in flow_stats and not (confirm_stats and symbol_analysis.symbol in confirm_stats)
                else []
            ),
            *(
                [f"24s: {market_stats[symbol_analysis.symbol].price_change_pct:+.1f}%"]
                if market_stats and symbol_analysis.symbol in market_stats
                and not (flow_stats and symbol_analysis.symbol in flow_stats)
                and not (confirm_stats and symbol_analysis.symbol in confirm_stats)
                else []
            ),
        ])

        for item in symbol_analysis.timeframes:
            lines.append(
                f"{_timeframe_label(item.timeframe)}: {_trend_label(item)} | "
                f"Güç {item.trend_score:+d} | RSI {item.rsi:.0f} | "
                f"{_decision(item, cost, altcoin_blocked)}"
            )

        lines.extend([
            f"4H Destek/Direnç: {_fmt_price(last_item.support)} / {_fmt_price(last_item.resistance)}",
            _trade_zones(last_item, altcoin_blocked),
            f"Alarm: {'; '.join(alarm_lines) if alarm_lines else 'Yok'}",
        ])
    return "\n".join(lines)
