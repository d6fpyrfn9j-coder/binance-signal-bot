"""Market data fetcher for Binance public candles."""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
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
    quote_volume: float
    trade_count: int
    taker_buy_base_volume: float
    taker_buy_quote_volume: float


@dataclass(frozen=True)
class MarketStat:
    symbol: str
    price_change_pct: float
    quote_volume: float
    last_price: float
    taker_buy_quote_volume: float = 0.0
    taker_sell_quote_volume: float = 0.0
    net_taker_quote_volume: float = 0.0


@dataclass(frozen=True)
class OrderBookPressure:
    symbol: str
    bid_quote_1pct: float
    ask_quote_1pct: float
    imbalance_pct: float
    nearest_buy_wall_price: float | None
    nearest_buy_wall_quote: float
    nearest_sell_wall_price: float | None
    nearest_sell_wall_quote: float


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
    logging.debug("Fetching candle data for %s", symbol)
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
                quote_volume=float(row[7]),
                trade_count=int(row[8]),
                taker_buy_base_volume=float(row[9]),
                taker_buy_quote_volume=float(row[10]),
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
        quote_volume = current.quote_volume or current.close * current.volume
        taker_buy_quote = current.taker_buy_quote_volume
        taker_sell_quote = max(quote_volume - taker_buy_quote, 0.0)
        stats[symbol] = MarketStat(
            symbol=symbol,
            price_change_pct=change_pct,
            quote_volume=quote_volume,
            last_price=current.close,
            taker_buy_quote_volume=taker_buy_quote,
            taker_sell_quote_volume=taker_sell_quote,
            net_taker_quote_volume=taker_buy_quote - taker_sell_quote,
        )
    return stats


def _wall(levels: list[tuple[float, float]]) -> tuple[float | None, float]:
    if not levels:
        return None, 0.0
    quotes = [(price, price * quantity) for price, quantity in levels]
    avg_quote = sum(quote for _, quote in quotes) / len(quotes)
    price, quote = max(quotes, key=lambda item: item[1])
    if avg_quote <= 0 or quote < avg_quote * 3:
        return None, 0.0
    return price, quote


def _fetch_order_book_pressure_one(symbol: str, limit: int = 100) -> OrderBookPressure | None:
    params = urllib.parse.urlencode({"symbol": symbol.upper(), "limit": limit})
    path = f"/api/v3/depth?{params}"
    logging.debug("Fetching %s order book", symbol)
    row = _http_json(path, timeout=10)

    bids = [(float(price), float(quantity)) for price, quantity in row.get("bids", [])]
    asks = [(float(price), float(quantity)) for price, quantity in row.get("asks", [])]
    if not bids or not asks:
        return None

    best_bid = bids[0][0]
    best_ask = asks[0][0]
    mid_price = (best_bid + best_ask) / 2
    bid_floor = mid_price * 0.99
    ask_ceiling = mid_price * 1.01
    nearby_bids = [(price, quantity) for price, quantity in bids if price >= bid_floor]
    nearby_asks = [(price, quantity) for price, quantity in asks if price <= ask_ceiling]

    bid_quote = sum(price * quantity for price, quantity in nearby_bids)
    ask_quote = sum(price * quantity for price, quantity in nearby_asks)
    total_quote = bid_quote + ask_quote
    imbalance_pct = ((bid_quote - ask_quote) / total_quote) * 100 if total_quote else 0.0
    buy_wall_price, buy_wall_quote = _wall(nearby_bids)
    sell_wall_price, sell_wall_quote = _wall(nearby_asks)

    return OrderBookPressure(
        symbol=symbol,
        bid_quote_1pct=bid_quote,
        ask_quote_1pct=ask_quote,
        imbalance_pct=imbalance_pct,
        nearest_buy_wall_price=buy_wall_price,
        nearest_buy_wall_quote=buy_wall_quote,
        nearest_sell_wall_price=sell_wall_price,
        nearest_sell_wall_quote=sell_wall_quote,
    )


def fetch_order_book_pressure(symbols: tuple[str, ...], limit: int = 100) -> dict[str, OrderBookPressure]:
    pressures: dict[str, OrderBookPressure] = {}
    workers = min(max(len(symbols), 1), 8)
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(_fetch_order_book_pressure_one, symbol, limit): symbol
            for symbol in symbols
        }
        for future in as_completed(futures):
            symbol = futures[future]
            try:
                pressure = future.result()
            except Exception:
                logging.exception("Could not fetch %s order book", symbol)
                continue
            if pressure:
                pressures[symbol] = pressure
    return pressures


def _fetch_recent_trade_flow_one(
    symbol: str,
    start_time_ms: int,
    end_time_ms: int,
) -> MarketStat | None:
    params = urllib.parse.urlencode(
        {
            "symbol": symbol.upper(),
            "startTime": start_time_ms,
            "endTime": end_time_ms,
            "limit": 1000,
        }
    )
    rows = _http_json(f"/api/v3/aggTrades?{params}", timeout=10)
    if not rows:
        return None

    taker_buy_quote = 0.0
    taker_sell_quote = 0.0
    first_price = float(rows[0].get("p", 0) or 0)
    last_price = first_price
    for row in rows:
        price = float(row.get("p", 0) or 0)
        quantity = float(row.get("q", 0) or 0)
        quote = price * quantity
        last_price = price
        if row.get("m"):
            taker_sell_quote += quote
        else:
            taker_buy_quote += quote

    change_pct = ((last_price - first_price) / first_price) * 100 if first_price else 0.0
    quote_volume = taker_buy_quote + taker_sell_quote
    return MarketStat(
        symbol=symbol,
        price_change_pct=change_pct,
        quote_volume=quote_volume,
        last_price=last_price,
        taker_buy_quote_volume=taker_buy_quote,
        taker_sell_quote_volume=taker_sell_quote,
        net_taker_quote_volume=taker_buy_quote - taker_sell_quote,
    )


def fetch_recent_trade_flow(
    symbols: tuple[str, ...],
    start_time_ms: int,
    end_time_ms: int,
) -> dict[str, MarketStat]:
    stats: dict[str, MarketStat] = {}
    workers = min(max(len(symbols), 1), 8)
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(_fetch_recent_trade_flow_one, symbol, start_time_ms, end_time_ms): symbol
            for symbol in symbols
        }
        for future in as_completed(futures):
            symbol = futures[future]
            try:
                stat = future.result()
            except Exception:
                logging.exception("Could not fetch %s trade flow", symbol)
                continue
            if stat:
                stats[symbol] = stat
    return stats
