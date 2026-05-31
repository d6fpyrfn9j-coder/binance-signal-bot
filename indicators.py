"""Technical indicators used by the analysis bot."""

from __future__ import annotations


def sma(values: list[float], period: int) -> float:
    if len(values) < period:
        raise ValueError(f"Need at least {period} values")
    return sum(values[-period:]) / period


def standard_deviation(values: list[float], period: int) -> float:
    if len(values) < period:
        raise ValueError(f"Need at least {period} values")
    recent = values[-period:]
    mean = sum(recent) / period
    variance = sum((value - mean) ** 2 for value in recent) / period
    return variance ** 0.5


def bollinger_bands(values: list[float], period: int = 20, deviations: float = 2.0) -> tuple[float, float, float]:
    middle = sma(values, period)
    width = standard_deviation(values, period) * deviations
    return middle - width, middle, middle + width


def momentum_pct(values: list[float], period: int = 10) -> float:
    if len(values) <= period:
        raise ValueError(f"Need more than {period} values")
    previous = values[-period - 1]
    if previous == 0:
        return 0.0
    return ((values[-1] - previous) / previous) * 100


def ema_series(values: list[float], period: int) -> list[float]:
    if len(values) < period:
        raise ValueError(f"Need at least {period} values")
    multiplier = 2 / (period + 1)
    current = sum(values[:period]) / period
    result = [current]
    for value in values[period:]:
        current = (value - current) * multiplier + current
        result.append(current)
    return result


def ema(values: list[float], period: int) -> float:
    return ema_series(values, period)[-1]


def rsi(values: list[float], period: int = 14) -> float:
    if len(values) <= period:
        raise ValueError(f"Need more than {period} values")

    gains: list[float] = []
    losses: list[float] = []
    for i in range(1, period + 1):
        delta = values[i] - values[i - 1]
        gains.append(max(delta, 0.0))
        losses.append(abs(min(delta, 0.0)))

    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period

    for i in range(period + 1, len(values)):
        delta = values[i] - values[i - 1]
        gain = max(delta, 0.0)
        loss = abs(min(delta, 0.0))
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period

    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def macd(values: list[float]) -> tuple[float, float, float]:
    if len(values) < 35:
        raise ValueError("Need at least 35 values for MACD")
    ema12 = ema_series(values, 12)
    ema26 = ema_series(values, 26)
    offset = len(ema12) - len(ema26)
    macd_line = [ema12[i + offset] - ema26[i] for i in range(len(ema26))]
    signal_line = ema_series(macd_line, 9)
    histogram = macd_line[-1] - signal_line[-1]
    return macd_line[-1], signal_line[-1], histogram


def previous_ema_pair(values: list[float], fast_period: int, slow_period: int) -> tuple[float, float]:
    if len(values) <= slow_period + 2:
        raise ValueError("Not enough values for previous EMA pair")
    return ema(values[:-1], fast_period), ema(values[:-1], slow_period)
