"""Market data fetcher for Binance public candles."""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass


DEFAULT_BINANCE_BASE_URLS = (
    "https://data-api.binance.vision",
    "https://api.binance.com",
)


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


def _base_urls() -> tuple[str, ...]:
    configured = os.getenv("BINANCE_BASE_URLS") or os.getenv("BINANCE_BASE_URL")
    if not configured:
        return DEFAULT_BINANCE_BASE_URLS

    urls = tuple(url.strip().rstrip("/") for url in configured.split(",") if url.strip())
    return urls or DEFAULT_BINANCE_BASE_URLS


def _read_json_url(url: str, timeout: int = 20):
    request = urllib.request.Request(url, headers={"User-Agent": "crypto-analysis-bot/1.0"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Network error: {exc.reason}") from exc


def _http_json(path: str, timeout: int = 20):
    errors: list[str] = []
    for base_url in _base_urls():
        url = f"{base_url.rstrip('/')}{path}"
        try:
            return _read_json_url(url, timeout=timeout)
        except RuntimeError as exc:
            errors.append(f"{base_url}: {exc}")
            logging.warning("Binance endpoint failed: %s", errors[-1])
    raise RuntimeError("All Binance market data endpoints failed: " + " | ".join(errors))


def fetch_binance_klines(symbol: str, interval: str, limit: int = 250) -> list[Candle]:
    params = urllib.parse.urlencode(
        {
            "symbol": symbol.upper(),
            "interval": interval,
            "limit": limit,
        }
    )
    path = f"/api/v3/klines?{params}"
    logging.info("Fetching %s %s candles", symbol, interval)
    rows = _http_json(path)

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
    rows = _http_json("/api/v3/ticker/24hr")
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
