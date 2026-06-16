"""Public multi-exchange ticker confirmation."""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass


DEFAULT_EXCHANGES = (
    "binance",
    "coinbase",
    "kraken",
    "okx",
    "bybit",
    "kucoin",
    "mexc",
)

SYMBOL_MAP = {
    "BTCUSDT": {
        "binance": "BTCUSDT",
        "coinbase": "BTC-USD",
        "kraken": "XBTUSD",
        "okx": "BTC-USDT",
        "bybit": "BTCUSDT",
        "kucoin": "BTC-USDT",
        "gate": "BTC_USDT",
        "mexc": "BTCUSDT",
    },
    "ETHUSDT": {
        "binance": "ETHUSDT",
        "coinbase": "ETH-USD",
        "kraken": "ETHUSD",
        "okx": "ETH-USDT",
        "bybit": "ETHUSDT",
        "kucoin": "ETH-USDT",
        "gate": "ETH_USDT",
        "mexc": "ETHUSDT",
    },
}


@dataclass(frozen=True)
class ExchangeTicker:
    symbol: str
    exchange: str
    price: float
    quote_volume_24h: float | None = None
    change_pct_24h: float | None = None


@dataclass(frozen=True)
class ExchangeConsensus:
    symbol: str
    tickers: tuple[ExchangeTicker, ...]

    @property
    def exchange_count(self) -> int:
        return len(self.tickers)

    @property
    def total_quote_volume_24h(self) -> float | None:
        volumes = [ticker.quote_volume_24h for ticker in self.tickers if ticker.quote_volume_24h]
        if not volumes:
            return None
        return sum(volumes)

    @property
    def weighted_price(self) -> float | None:
        weighted = [
            (ticker.price, ticker.quote_volume_24h or 0.0)
            for ticker in self.tickers
            if ticker.price > 0
        ]
        if not weighted:
            return None
        volume_sum = sum(volume for _, volume in weighted)
        if volume_sum <= 0:
            return sum(price for price, _ in weighted) / len(weighted)
        return sum(price * volume for price, volume in weighted) / volume_sum

    @property
    def spread_pct(self) -> float | None:
        prices = [ticker.price for ticker in self.tickers if ticker.price > 0]
        if len(prices) < 2:
            return None
        reference = self.weighted_price or (sum(prices) / len(prices))
        if reference <= 0:
            return None
        return ((max(prices) - min(prices)) / reference) * 100

    @property
    def exchanges(self) -> tuple[str, ...]:
        return tuple(ticker.exchange for ticker in self.tickers)


def _enabled() -> bool:
    value = os.getenv("MULTI_EXCHANGE_ENABLED", "true").strip().lower()
    return value not in {"0", "false", "no", "off"}


def _configured_exchanges() -> tuple[str, ...]:
    configured = os.getenv("MULTI_EXCHANGE_LIST")
    if not configured:
        return DEFAULT_EXCHANGES
    exchanges = tuple(
        exchange.strip().lower()
        for exchange in configured.split(",")
        if exchange.strip()
    )
    return exchanges or DEFAULT_EXCHANGES


def _number(value) -> float | None:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _read_json(url: str, timeout: int = 8):
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "binance-signal-bot/1.0"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Network error: {exc.reason}") from exc


def _binance(symbol: str, pair: str) -> ExchangeTicker:
    params = urllib.parse.urlencode({"symbol": pair})
    row = _read_json(f"https://api.binance.com/api/v3/ticker/24hr?{params}")
    price = _number(row.get("lastPrice")) or 0.0
    return ExchangeTicker(
        symbol=symbol,
        exchange="Binance",
        price=price,
        quote_volume_24h=_number(row.get("quoteVolume")),
        change_pct_24h=_number(row.get("priceChangePercent")),
    )


def _coinbase(symbol: str, pair: str) -> ExchangeTicker:
    ticker = _read_json(f"https://api.exchange.coinbase.com/products/{pair}/ticker")
    price = _number(ticker.get("price")) or 0.0
    base_volume = _number(ticker.get("volume"))
    return ExchangeTicker(
        symbol=symbol,
        exchange="Coinbase",
        price=price,
        quote_volume_24h=base_volume * price if base_volume and price else None,
    )


def _kraken(symbol: str, pair: str) -> ExchangeTicker:
    params = urllib.parse.urlencode({"pair": pair})
    payload = _read_json(f"https://api.kraken.com/0/public/Ticker?{params}")
    result = payload.get("result", {})
    row = next(iter(result.values()))
    price = _number((row.get("c") or [None])[0]) or 0.0
    base_volume = _number((row.get("v") or [None, None])[1])
    open_price = _number(row.get("o"))
    change = ((price - open_price) / open_price) * 100 if price and open_price else None
    return ExchangeTicker(
        symbol=symbol,
        exchange="Kraken",
        price=price,
        quote_volume_24h=base_volume * price if base_volume and price else None,
        change_pct_24h=change,
    )


def _okx(symbol: str, pair: str) -> ExchangeTicker:
    params = urllib.parse.urlencode({"instId": pair})
    payload = _read_json(f"https://www.okx.com/api/v5/market/ticker?{params}")
    row = (payload.get("data") or [{}])[0]
    price = _number(row.get("last")) or 0.0
    quote_volume = _number(row.get("volCcy24h"))
    if quote_volume is None:
        base_volume = _number(row.get("vol24h"))
        quote_volume = base_volume * price if base_volume and price else None
    open_price = _number(row.get("open24h"))
    change = ((price - open_price) / open_price) * 100 if price and open_price else None
    return ExchangeTicker(
        symbol=symbol,
        exchange="OKX",
        price=price,
        quote_volume_24h=quote_volume,
        change_pct_24h=change,
    )


def _bybit(symbol: str, pair: str) -> ExchangeTicker:
    params = urllib.parse.urlencode({"category": "spot", "symbol": pair})
    payload = _read_json(f"https://api.bybit.com/v5/market/tickers?{params}")
    row = (payload.get("result", {}).get("list") or [{}])[0]
    price = _number(row.get("lastPrice")) or 0.0
    change = _number(row.get("price24hPcnt"))
    return ExchangeTicker(
        symbol=symbol,
        exchange="Bybit",
        price=price,
        quote_volume_24h=_number(row.get("turnover24h")),
        change_pct_24h=change * 100 if change is not None else None,
    )


def _kucoin(symbol: str, pair: str) -> ExchangeTicker:
    params = urllib.parse.urlencode({"symbol": pair})
    payload = _read_json(f"https://api.kucoin.com/api/v1/market/stats?{params}")
    row = payload.get("data", {})
    price = _number(row.get("last")) or 0.0
    quote_volume = _number(row.get("volValue"))
    if quote_volume is None:
        base_volume = _number(row.get("vol"))
        quote_volume = base_volume * price if base_volume and price else None
    return ExchangeTicker(
        symbol=symbol,
        exchange="KuCoin",
        price=price,
        quote_volume_24h=quote_volume,
        change_pct_24h=_number(row.get("changeRate")) * 100 if _number(row.get("changeRate")) is not None else None,
    )


def _gate(symbol: str, pair: str) -> ExchangeTicker:
    params = urllib.parse.urlencode({"currency_pair": pair})
    rows = _read_json(f"https://api.gateio.ws/api/v4/spot/tickers?{params}")
    row = rows[0] if isinstance(rows, list) and rows else {}
    price = _number(row.get("last")) or 0.0
    return ExchangeTicker(
        symbol=symbol,
        exchange="Gate",
        price=price,
        quote_volume_24h=_number(row.get("quote_volume")),
        change_pct_24h=_number(row.get("change_percentage")),
    )


def _mexc(symbol: str, pair: str) -> ExchangeTicker:
    params = urllib.parse.urlencode({"symbol": pair})
    row = _read_json(f"https://api.mexc.com/api/v3/ticker/24hr?{params}")
    price = _number(row.get("lastPrice")) or 0.0
    return ExchangeTicker(
        symbol=symbol,
        exchange="MEXC",
        price=price,
        quote_volume_24h=_number(row.get("quoteVolume")),
        change_pct_24h=_number(row.get("priceChangePercent")),
    )


FETCHERS = {
    "binance": _binance,
    "coinbase": _coinbase,
    "kraken": _kraken,
    "okx": _okx,
    "bybit": _bybit,
    "kucoin": _kucoin,
    "gate": _gate,
    "mexc": _mexc,
}


def _fetch_one(symbol: str, exchange: str) -> ExchangeTicker | None:
    pair = SYMBOL_MAP.get(symbol, {}).get(exchange)
    fetcher = FETCHERS.get(exchange)
    if not pair or not fetcher:
        return None
    ticker = fetcher(symbol, pair)
    if ticker.price <= 0:
        return None
    return ticker


def fetch_exchange_consensus(symbols: tuple[str, ...]) -> dict[str, ExchangeConsensus]:
    if not _enabled():
        return {}

    exchanges = _configured_exchanges()
    jobs = [
        (symbol, exchange)
        for symbol in symbols
        for exchange in exchanges
        if exchange in SYMBOL_MAP.get(symbol, {})
    ]
    results: dict[str, list[ExchangeTicker]] = {symbol: [] for symbol in symbols}
    if not jobs:
        return {}

    workers = min(max(len(jobs), 1), 12)
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(_fetch_one, symbol, exchange): (symbol, exchange)
            for symbol, exchange in jobs
        }
        for future in as_completed(futures):
            symbol, exchange = futures[future]
            try:
                ticker = future.result()
            except Exception as exc:
                logging.warning("Could not fetch %s ticker from %s: %s", symbol, exchange, exc)
                continue
            if ticker:
                results[symbol].append(ticker)

    return {
        symbol: ExchangeConsensus(
            symbol=symbol,
            tickers=tuple(sorted(tickers, key=lambda item: item.exchange)),
        )
        for symbol, tickers in results.items()
        if tickers
    }
