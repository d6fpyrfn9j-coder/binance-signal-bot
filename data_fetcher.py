"""Market data fetcher for Binance public candles."""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass


BINANCE_BASE_URL = "https://api.binance.com"


@dataclass(frozen=True)
class Candle:
    open_time: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    close_time: int


@dataclass(frozen=True)
class MarketStat:
    symbol: str
    price_change_pct: float
    quote_volume: float
    last_price: float


def _http_json(url: str, timeout: int = 20):
    request = urllib.request.Request(url, headers={"User-Agent": "crypto-analysis-bot/1.0"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Network error: {exc.reason}") from exc


def fetch_binance_klines(symbol: str, interval: str, limit: int = 250) -> list[Candle]:
    params = urllib.parse.urlencode(
        {
            "symbol": symbol.upper(),
            "interval": interval,
            "limit": limit,
        }
    )
    url = f"{BINANCE_BASE_URL}/api/v3/klines?{params}"
    logging.info("Fetching %s %s candles", symbol, interval)
    rows = _http_json(url)

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
            )
        )
    return candles


def fetch_24hr_stats(symbols: tuple[str, ...]) -> dict[str, MarketStat]:
    url = f"{BINANCE_BASE_URL}/api/v3/ticker/24hr"
    rows = _http_json(url)
    wanted = set(symbols)
    stats: dict[str, MarketStat] = {}
    for row in rows:
        symbol = row.get("symbol")
        if symbol not in wanted:
            continue
        stats[symbol] = MarketStat(
            symbol=symbol,
            price_change_pct=float(row.get("priceChangePercent", 0) or 0),
            quote_volume=float(row.get("quoteVolume", 0) or 0),
            last_price=float(row.get("lastPrice", 0) or 0),
        )
    return stats


def fetch_recent_flow_stats(symbols: tuple[str, ...], interval: str = "1h") -> dict[str, MarketStat]:
    stats: dict[str, MarketStat] = {}
    for symbol in symbols:
        candles = fetch_binance_klines(symbol, interval, limit=2)
        if len(candles) < 2:
            continue
        previous = candles[-2]
        current = candles[-1]
        change_pct = ((current.close - previous.close) / previous.close) * 100 if previous.close else 0.0
        quote_volume = current.close * current.volume
        stats[symbol] = MarketStat(
            symbol=symbol,
            price_change_pct=change_pct,
            quote_volume=quote_volume,
            last_price=current.close,
        )
    return stats
