"""Binance Futures smart-money flow analysis."""

from __future__ import annotations

import bisect
import json
import logging
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass


BINANCE_FUTURES_BASE_URL = os.getenv("BINANCE_FUTURES_BASE_URL", "https://fapi.binance.com")
FLOW_PERIOD = os.getenv("FUTURES_FLOW_PERIOD", "15m")
FLOW_LIMIT = int(os.getenv("FUTURES_FLOW_LIMIT", "24"))
PERIOD_MS = {
    "5m": 5 * 60 * 1000,
    "15m": 15 * 60 * 1000,
    "30m": 30 * 60 * 1000,
    "1h": 60 * 60 * 1000,
    "2h": 2 * 60 * 60 * 1000,
    "4h": 4 * 60 * 60 * 1000,
    "6h": 6 * 60 * 60 * 1000,
    "12h": 12 * 60 * 60 * 1000,
    "1d": 24 * 60 * 60 * 1000,
}


@dataclass(frozen=True)
class RatioPoint:
    timestamp: int
    long_short_ratio: float | None
    long_pct: float | None
    short_pct: float | None


@dataclass(frozen=True)
class OpenInterestPoint:
    timestamp: int
    open_interest: float | None
    open_interest_value: float | None


@dataclass(frozen=True)
class FundingPoint:
    timestamp: int
    funding_rate_pct: float | None


@dataclass(frozen=True)
class FuturesFlowSnapshot:
    symbol: str
    timestamp: int | None
    open_interest: float | None
    open_interest_value: float | None
    oi_change_pct: float | None
    funding_rate_pct: float | None
    global_long_pct: float | None
    global_short_pct: float | None
    global_long_short_ratio: float | None
    top_long_pct: float | None
    top_short_pct: float | None
    top_long_short_ratio: float | None
    crowding: str
    smart_money_bias: str
    long_score: int
    short_score: int
    reason: str


@dataclass(frozen=True)
class FuturesFlowHistory:
    symbol: str
    open_interest: tuple[OpenInterestPoint, ...] = ()
    funding: tuple[FundingPoint, ...] = ()
    global_ratio: tuple[RatioPoint, ...] = ()
    top_ratio: tuple[RatioPoint, ...] = ()


def _read_json(path: str, params: dict[str, object], timeout: int = 15):
    url = f"{BINANCE_FUTURES_BASE_URL.rstrip('/')}{path}?{urllib.parse.urlencode(params)}"
    request = urllib.request.Request(url, headers={"User-Agent": "binance-futures-flow/1.0"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Network error: {exc.reason}") from exc


def _safe_rows(path: str, params: dict[str, object]) -> list[dict[str, object]]:
    try:
        rows = _read_json(path, params)
    except Exception as exc:
        logging.debug("Futures flow endpoint failed %s %s: %s", path, params.get("symbol"), exc)
        return []
    return rows if isinstance(rows, list) else []


def _as_float(value: object) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_int(value: object) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _pct_change(current: float | None, previous: float | None) -> float | None:
    if current is None or previous is None or previous == 0:
        return None
    return ((current - previous) / previous) * 100


def _parse_oi(rows: list[dict[str, object]]) -> tuple[OpenInterestPoint, ...]:
    points: list[OpenInterestPoint] = []
    for row in rows:
        timestamp = _as_int(row.get("timestamp"))
        if timestamp is None:
            continue
        points.append(
            OpenInterestPoint(
                timestamp=timestamp,
                open_interest=_as_float(row.get("sumOpenInterest")),
                open_interest_value=_as_float(row.get("sumOpenInterestValue")),
            )
        )
    return tuple(sorted(points, key=lambda point: point.timestamp))


def _parse_funding(rows: list[dict[str, object]]) -> tuple[FundingPoint, ...]:
    points: list[FundingPoint] = []
    for row in rows:
        timestamp = _as_int(row.get("fundingTime"))
        rate = _as_float(row.get("fundingRate"))
        if timestamp is None:
            continue
        points.append(
            FundingPoint(
                timestamp=timestamp,
                funding_rate_pct=rate * 100 if rate is not None else None,
            )
        )
    return tuple(sorted(points, key=lambda point: point.timestamp))


def _parse_ratio(rows: list[dict[str, object]]) -> tuple[RatioPoint, ...]:
    points: list[RatioPoint] = []
    for row in rows:
        timestamp = _as_int(row.get("timestamp"))
        if timestamp is None:
            continue
        points.append(
            RatioPoint(
                timestamp=timestamp,
                long_short_ratio=_as_float(row.get("longShortRatio")),
                long_pct=_as_float(row.get("longAccount")),
                short_pct=_as_float(row.get("shortAccount")),
            )
        )
    return tuple(sorted(points, key=lambda point: point.timestamp))


def _fetch_period_history(
    path: str,
    symbol: str,
    start_ms: int,
    end_ms: int,
    period: str,
    limit: int = 500,
) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    cursor = start_ms
    step_ms = PERIOD_MS.get(period, PERIOD_MS["15m"])
    while cursor <= end_ms:
        chunk = _safe_rows(
            path,
            {
                "symbol": symbol,
                "period": period,
                "startTime": cursor,
                "endTime": end_ms,
                "limit": limit,
            },
        )
        if not chunk:
            break
        rows.extend(chunk)
        last_time = _as_int(chunk[-1].get("timestamp"))
        if last_time is None or last_time < cursor:
            break
        cursor = last_time + step_ms
        if len(chunk) < limit:
            break
        time.sleep(0.03)
    return rows


def _latest_before(points: tuple, timestamp: int):
    if not points:
        return None
    timestamps = [point.timestamp for point in points]
    index = bisect.bisect_right(timestamps, timestamp) - 1
    if index < 0:
        return None
    return points[index]


def _oi_change(points: tuple[OpenInterestPoint, ...], timestamp: int | None = None, lookback: int = 4) -> tuple[OpenInterestPoint | None, float | None]:
    if not points:
        return None, None
    if timestamp is None:
        index = len(points) - 1
    else:
        timestamps = [point.timestamp for point in points]
        index = bisect.bisect_right(timestamps, timestamp) - 1
    if index < 0:
        return None, None
    current = points[index]
    previous = points[max(0, index - lookback)]
    current_value = current.open_interest_value or current.open_interest
    previous_value = previous.open_interest_value or previous.open_interest
    return current, _pct_change(current_value, previous_value)


def _crowding(global_ratio: RatioPoint | None, top_ratio: RatioPoint | None) -> str:
    long_pct = global_ratio.long_pct if global_ratio else None
    short_pct = global_ratio.short_pct if global_ratio else None
    ratio = global_ratio.long_short_ratio if global_ratio else None
    top_long = top_ratio.long_pct if top_ratio else None
    top_short = top_ratio.short_pct if top_ratio else None
    top_ls = top_ratio.long_short_ratio if top_ratio else None

    if (long_pct is not None and long_pct >= 0.62) or (ratio is not None and ratio >= 1.65):
        return "LONG_CROWDED"
    if (short_pct is not None and short_pct >= 0.58) or (ratio is not None and ratio <= 0.72):
        return "SHORT_CROWDED"
    if (top_long is not None and top_long >= 0.64) or (top_ls is not None and top_ls >= 1.75):
        return "TOP_LONG_CROWDED"
    if (top_short is not None and top_short >= 0.58) or (top_ls is not None and top_ls <= 0.72):
        return "TOP_SHORT_CROWDED"
    return "NEUTRAL"


def analyze_futures_flow(
    symbol: str,
    oi_points: tuple[OpenInterestPoint, ...] = (),
    funding_points: tuple[FundingPoint, ...] = (),
    global_points: tuple[RatioPoint, ...] = (),
    top_points: tuple[RatioPoint, ...] = (),
    price_change_pct: float = 0.0,
    volume_momentum_pct: float = 0.0,
    timestamp: int | None = None,
) -> FuturesFlowSnapshot:
    current_oi, oi_change_pct = _oi_change(oi_points, timestamp)
    funding = _latest_before(funding_points, timestamp) if timestamp is not None else (funding_points[-1] if funding_points else None)
    global_ratio = _latest_before(global_points, timestamp) if timestamp is not None else (global_points[-1] if global_points else None)
    top_ratio = _latest_before(top_points, timestamp) if timestamp is not None else (top_points[-1] if top_points else None)
    funding_pct = funding.funding_rate_pct if funding else None
    crowding = _crowding(global_ratio, top_ratio)

    long_score = 0
    short_score = 0
    reasons: list[str] = []

    if price_change_pct >= 0.8 and oi_change_pct is not None and oi_change_pct >= 6:
        short_score += 12
        reasons.append("pump + OI overheated")
    if funding_pct is not None and funding_pct >= 0.05:
        short_score += 10
        reasons.append("positive funding extreme")
    elif funding_pct is not None and funding_pct >= 0.025:
        short_score += 5
        reasons.append("funding positive")

    if crowding in {"LONG_CROWDED", "TOP_LONG_CROWDED"}:
        short_score += 10
        reasons.append("longs crowded")
    if price_change_pct > 0 and volume_momentum_pct <= -10:
        short_score += 6
        reasons.append("volume momentum fades")

    if crowding in {"SHORT_CROWDED", "TOP_SHORT_CROWDED"}:
        long_score += 12
        reasons.append("shorts crowded")
    if funding_pct is not None and funding_pct <= -0.05:
        long_score += 10
        reasons.append("negative funding extreme")
    elif funding_pct is not None and funding_pct <= -0.025:
        long_score += 5
        reasons.append("funding negative")
    if price_change_pct >= 0.25 and oi_change_pct is not None and 1.5 <= oi_change_pct <= 8:
        long_score += 8
        reasons.append("OI supports uptrend")
    if price_change_pct <= -0.25 and oi_change_pct is not None and oi_change_pct <= -1:
        long_score += 4
        reasons.append("OI flush")

    if long_score >= short_score + 5:
        bias = "LONG"
    elif short_score >= long_score + 5:
        bias = "SHORT"
    else:
        bias = "NEUTRAL"

    return FuturesFlowSnapshot(
        symbol=symbol,
        timestamp=timestamp or (
            max(
                [point.timestamp for point in (*oi_points, *funding_points, *global_points, *top_points)],
                default=None,
            )
        ),
        open_interest=current_oi.open_interest if current_oi else None,
        open_interest_value=current_oi.open_interest_value if current_oi else None,
        oi_change_pct=oi_change_pct,
        funding_rate_pct=funding_pct,
        global_long_pct=global_ratio.long_pct if global_ratio else None,
        global_short_pct=global_ratio.short_pct if global_ratio else None,
        global_long_short_ratio=global_ratio.long_short_ratio if global_ratio else None,
        top_long_pct=top_ratio.long_pct if top_ratio else None,
        top_short_pct=top_ratio.short_pct if top_ratio else None,
        top_long_short_ratio=top_ratio.long_short_ratio if top_ratio else None,
        crowding=crowding,
        smart_money_bias=bias,
        long_score=long_score,
        short_score=short_score,
        reason=", ".join(reasons) if reasons else "neutral futures flow",
    )


def neutral_futures_flow(symbol: str, reason: str = "futures flow unavailable") -> FuturesFlowSnapshot:
    return FuturesFlowSnapshot(
        symbol=symbol,
        timestamp=None,
        open_interest=None,
        open_interest_value=None,
        oi_change_pct=None,
        funding_rate_pct=None,
        global_long_pct=None,
        global_short_pct=None,
        global_long_short_ratio=None,
        top_long_pct=None,
        top_short_pct=None,
        top_long_short_ratio=None,
        crowding="UNKNOWN",
        smart_money_bias="NEUTRAL",
        long_score=0,
        short_score=0,
        reason=reason,
    )


def fetch_futures_flow(
    symbol: str,
    price_change_pct: float = 0.0,
    volume_momentum_pct: float = 0.0,
    period: str = FLOW_PERIOD,
    limit: int = FLOW_LIMIT,
) -> FuturesFlowSnapshot:
    symbol = symbol.upper()
    oi_rows = _safe_rows("/futures/data/openInterestHist", {"symbol": symbol, "period": period, "limit": limit})
    funding_rows = _safe_rows("/fapi/v1/fundingRate", {"symbol": symbol, "limit": min(max(limit, 2), 1000)})
    global_rows = _safe_rows("/futures/data/globalLongShortAccountRatio", {"symbol": symbol, "period": period, "limit": limit})
    top_rows = _safe_rows("/futures/data/topLongShortPositionRatio", {"symbol": symbol, "period": period, "limit": limit})
    if not top_rows:
        top_rows = _safe_rows("/futures/data/topLongShortAccountRatio", {"symbol": symbol, "period": period, "limit": limit})

    if not any((oi_rows, funding_rows, global_rows, top_rows)):
        return neutral_futures_flow(symbol)
    return analyze_futures_flow(
        symbol=symbol,
        oi_points=_parse_oi(oi_rows),
        funding_points=_parse_funding(funding_rows),
        global_points=_parse_ratio(global_rows),
        top_points=_parse_ratio(top_rows),
        price_change_pct=price_change_pct,
        volume_momentum_pct=volume_momentum_pct,
    )


def _fetch_funding_history(symbol: str, start_ms: int, end_ms: int) -> tuple[FundingPoint, ...]:
    rows: list[dict[str, object]] = []
    cursor = start_ms
    while cursor <= end_ms:
        chunk = _safe_rows(
            "/fapi/v1/fundingRate",
            {"symbol": symbol, "startTime": cursor, "endTime": end_ms, "limit": 1000},
        )
        if not chunk:
            break
        rows.extend(chunk)
        last_time = _as_int(chunk[-1].get("fundingTime"))
        if last_time is None or last_time < cursor:
            break
        cursor = last_time + 1
        time.sleep(0.03)
    return _parse_funding(rows)


def fetch_futures_flow_history(
    symbol: str,
    start_ms: int,
    end_ms: int,
    period: str = FLOW_PERIOD,
) -> FuturesFlowHistory:
    symbol = symbol.upper()
    now_ms = int(time.time() * 1000)
    recent_cutoff = now_ms - 29 * 24 * 60 * 60 * 1000
    oi_points: tuple[OpenInterestPoint, ...] = ()
    global_points: tuple[RatioPoint, ...] = ()
    top_points: tuple[RatioPoint, ...] = ()

    if end_ms >= recent_cutoff:
        bounded_start = max(start_ms, recent_cutoff)
        oi_points = _parse_oi(
            _fetch_period_history("/futures/data/openInterestHist", symbol, bounded_start, end_ms, period)
        )
        global_points = _parse_ratio(
            _fetch_period_history("/futures/data/globalLongShortAccountRatio", symbol, bounded_start, end_ms, period)
        )
        top_rows = _fetch_period_history("/futures/data/topLongShortPositionRatio", symbol, bounded_start, end_ms, period)
        if not top_rows:
            top_rows = _fetch_period_history("/futures/data/topLongShortAccountRatio", symbol, bounded_start, end_ms, period)
        top_points = _parse_ratio(top_rows)

    return FuturesFlowHistory(
        symbol=symbol,
        open_interest=oi_points,
        funding=_fetch_funding_history(symbol, start_ms, end_ms),
        global_ratio=global_points,
        top_ratio=top_points,
    )


def futures_flow_at(
    history: FuturesFlowHistory | None,
    timestamp: int,
    price_change_pct: float,
    volume_momentum_pct: float,
) -> FuturesFlowSnapshot | None:
    if history is None:
        return None
    if not any((history.open_interest, history.funding, history.global_ratio, history.top_ratio)):
        return None
    return analyze_futures_flow(
        symbol=history.symbol,
        oi_points=history.open_interest,
        funding_points=history.funding,
        global_points=history.global_ratio,
        top_points=history.top_ratio,
        price_change_pct=price_change_pct,
        volume_momentum_pct=volume_momentum_pct,
        timestamp=timestamp,
    )
