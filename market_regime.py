"""Market regime classification for BTC-led futures signals."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from data_fetcher import Candle
from indicators import ema, rsi


class MarketRegime(str, Enum):
    BULL = "BULL"
    BEAR = "BEAR"
    RANGE = "RANGE"
    LATE_BULL = "LATE_BULL"


@dataclass(frozen=True)
class RegimeFrame:
    timeframe: str
    close: float
    ema50: float
    ema200: float
    ema50_slope_pct: float
    volatility_pct: float
    trend_score: int = 0
    rsi: float | None = None
    momentum_pct: float = 0.0
    support: float | None = None
    resistance: float | None = None


@dataclass(frozen=True)
class MarketRegimeResult:
    regime: MarketRegime
    confidence: int
    reason: str
    btc_dominance_pct: float | None = None
    min_rr: float = 1.8
    leverage_note: str = "Kaldıraç: normal"
    warning: str | None = None


def _pct_change(current: float, previous: float) -> float:
    if previous == 0:
        return 0.0
    return ((current - previous) / previous) * 100


def _atr_pct(candles: list[Candle], period: int = 14) -> float:
    if len(candles) <= period:
        return 0.0

    true_ranges: list[float] = []
    recent = candles[-period:]
    previous_close = candles[-period - 1].close
    for candle in recent:
        true_range = max(
            candle.high - candle.low,
            abs(candle.high - previous_close),
            abs(candle.low - previous_close),
        )
        true_ranges.append(true_range)
        previous_close = candle.close

    close = candles[-1].close
    if close <= 0:
        return 0.0
    return (sum(true_ranges) / len(true_ranges) / close) * 100


def regime_frame_from_candles(timeframe: str, candles: list[Candle]) -> RegimeFrame:
    if len(candles) < 210:
        raise ValueError(f"{timeframe} regime icin en az 210 mum gerekli")

    closes = [candle.close for candle in candles]
    ema50_now = ema(closes, 50)
    ema50_prev = ema(closes[:-1], 50)
    ema200_now = ema(closes, 200)
    recent = candles[-30:] if len(candles) >= 30 else candles
    support = min(candle.low for candle in recent)
    resistance = max(candle.high for candle in recent)
    return RegimeFrame(
        timeframe=timeframe,
        close=closes[-1],
        ema50=ema50_now,
        ema200=ema200_now,
        ema50_slope_pct=_pct_change(ema50_now, ema50_prev),
        volatility_pct=_atr_pct(candles),
        rsi=rsi(closes),
        momentum_pct=_pct_change(closes[-1], closes[-11]) if len(closes) > 10 else 0.0,
        support=support,
        resistance=resistance,
    )


def range_position(close: float, support: float | None, resistance: float | None) -> float | None:
    if support is None or resistance is None or resistance <= support:
        return None
    return (close - support) / (resistance - support)


def is_near_middle_of_range(
    close: float,
    support: float | None,
    resistance: float | None,
    middle_band: float = 0.12,
) -> bool:
    position = range_position(close, support, resistance)
    if position is None:
        return False
    return abs(position - 0.5) <= middle_band


def _dominance_note(btc_dominance_pct: float | None) -> str | None:
    if btc_dominance_pct is None:
        return None
    if btc_dominance_pct >= 58:
        return "BTC.D yüksek"
    if btc_dominance_pct <= 48:
        return "BTC.D düşük"
    return None


def _is_bullish(frame: RegimeFrame) -> bool:
    return (
        frame.close > frame.ema200
        and frame.ema50 > frame.ema200
        and frame.ema50_slope_pct >= -0.03
        and frame.trend_score >= -1
    )


def _is_bearish(frame: RegimeFrame) -> bool:
    return (
        frame.close < frame.ema200
        and frame.ema50 < frame.ema200
        and frame.ema50_slope_pct <= 0.03
        and frame.trend_score <= 1
    )


def _is_range(frame: RegimeFrame) -> bool:
    near_ema200 = abs(_pct_change(frame.close, frame.ema200)) <= 3.5
    low_slope = abs(frame.ema50_slope_pct) <= 0.08
    neutral_trend = -3 <= frame.trend_score <= 3
    moderate_volatility = frame.volatility_pct <= 4.5
    return sum((near_ema200, low_slope, neutral_trend, moderate_volatility)) >= 3


def _is_late_bull(
    frame_4h: RegimeFrame,
    frame_1d: RegimeFrame | None,
    btc_dominance_pct: float | None,
) -> bool:
    base = frame_1d or frame_4h
    if not _is_bullish(frame_4h) or not _is_bullish(base):
        return False

    stretched_50 = _pct_change(frame_4h.close, frame_4h.ema50) >= 6
    stretched_200 = _pct_change(base.close, base.ema200) >= 22
    overheated = bool((frame_4h.rsi or 0) >= 68 or (base.rsi or 0) >= 68)
    volatility_spike = frame_4h.volatility_pct >= 5.5 and frame_4h.momentum_pct <= 0
    dominance_extreme = btc_dominance_pct is not None and (
        btc_dominance_pct >= 58 or btc_dominance_pct <= 48
    )
    return (stretched_50 and overheated) or stretched_200 or volatility_spike or dominance_extreme


def classify_market_regime(
    frame_4h: RegimeFrame | None,
    frame_1d: RegimeFrame | None = None,
    btc_dominance_pct: float | None = None,
) -> MarketRegimeResult:
    if frame_4h is None:
        return MarketRegimeResult(
            regime=MarketRegime.RANGE,
            confidence=35,
            reason="BTC 4h verisi yok",
            btc_dominance_pct=btc_dominance_pct,
            min_rr=2.5,
        )

    daily = frame_1d or frame_4h
    dominance = _dominance_note(btc_dominance_pct)
    bull_confirmed = _is_bullish(frame_4h) and _is_bullish(daily)
    bear_confirmed = _is_bearish(frame_4h) and (
        _is_bearish(daily) or frame_4h.trend_score <= -5
    )

    if bear_confirmed:
        reason = "BTC EMA50/EMA200 altinda, egim zayif"
        if dominance:
            reason = f"{reason}; {dominance}"
        return MarketRegimeResult(
            regime=MarketRegime.BEAR,
            confidence=82,
            reason=reason,
            btc_dominance_pct=btc_dominance_pct,
            leverage_note="Kaldıraç: temkinli",
        )

    if _is_late_bull(frame_4h, frame_1d, btc_dominance_pct):
        reason = "BTC trend yukari ama uzamis/volatil"
        if dominance:
            reason = f"{reason}; {dominance}"
        return MarketRegimeResult(
            regime=MarketRegime.LATE_BULL,
            confidence=76,
            reason=reason,
            btc_dominance_pct=btc_dominance_pct,
            leverage_note="Kaldıraç: düşük",
            warning="kâr alımı riski",
        )

    if bull_confirmed:
        reason = "BTC EMA200 uzeri, EMA50 egimi pozitif"
        if dominance:
            reason = f"{reason}; {dominance}"
        return MarketRegimeResult(
            regime=MarketRegime.BULL,
            confidence=80,
            reason=reason,
            btc_dominance_pct=btc_dominance_pct,
            leverage_note="Kaldıraç: normal",
        )

    if _is_range(frame_4h) or _is_range(daily):
        return MarketRegimeResult(
            regime=MarketRegime.RANGE,
            confidence=70,
            reason="BTC EMA200 cevresinde/yatay egim",
            btc_dominance_pct=btc_dominance_pct,
            min_rr=2.5,
            leverage_note="Kaldıraç: düşük",
            warning="orta bantta işlem yok",
        )

    return MarketRegimeResult(
        regime=MarketRegime.RANGE,
        confidence=58,
        reason="rejim karisik, teyit bekle",
        btc_dominance_pct=btc_dominance_pct,
        min_rr=2.5,
        leverage_note="Kaldıraç: düşük",
        warning="net trend yok",
    )
