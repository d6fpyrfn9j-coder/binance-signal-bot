#!/usr/bin/env python3
"""Hourly crypto market analysis bot."""

from __future__ import annotations

import argparse
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from analyzer import analyze_symbol, build_report
from data_fetcher import (
    MarketStat,
    OrderBookPressure,
    fetch_24hr_stats,
    fetch_binance_klines,
    fetch_order_book_pressure,
    fetch_recent_flow_stats,
    fetch_recent_trade_flow,
)
from onchain_fetcher import OnChainFlow, StablecoinReserve, fetch_onchain_flows, fetch_stablecoin_reserve
from telegram_sender import send_telegram_message


CORE_SYMBOLS = (
    "BTCUSDT",
    "ETHUSDT",
    "SOLUSDT",
)
ALT_CANDIDATES = (
    "FETUSDT",
    "RENDERUSDT",
    "ONDOUSDT",
    "FILUSDT",
    "ARBUSDT",
    "OPUSDT",
)
ALL_SYMBOLS = CORE_SYMBOLS + ALT_CANDIDATES
TIMEFRAMES = ("15m", "1h", "4h")
MAX_ALT_SYMBOLS = 3
MIN_ALT_QUOTE_VOLUME = 5_000_000
MIN_ALT_5M_QUOTE_VOLUME = 30_000
SECTORS = {
    "BTCUSDT": "BTC",
    "ETHUSDT": "Layer 1",
    "SOLUSDT": "Layer 1",
    "FETUSDT": "AI",
    "RENDERUSDT": "AI / DePIN",
    "ONDOUSDT": "RWA",
    "FILUSDT": "DePIN",
    "ARBUSDT": "Layer 2",
    "OPUSDT": "Layer 2",
}


@dataclass
class FlowAccumulator:
    symbol: str
    taker_buy_quote_volume: float = 0.0
    taker_sell_quote_volume: float = 0.0
    first_price: float | None = None
    last_price: float | None = None

    def add(self, stat: MarketStat) -> None:
        if self.first_price is None and stat.last_price > 0:
            self.first_price = stat.last_price
        if stat.last_price > 0:
            self.last_price = stat.last_price
        self.taker_buy_quote_volume += stat.taker_buy_quote_volume
        self.taker_sell_quote_volume += stat.taker_sell_quote_volume

    def to_market_stat(self) -> MarketStat | None:
        quote_volume = self.taker_buy_quote_volume + self.taker_sell_quote_volume
        if quote_volume <= 0 or self.last_price is None:
            return None
        first_price = self.first_price or self.last_price
        change_pct = ((self.last_price - first_price) / first_price) * 100 if first_price else 0.0
        return MarketStat(
            symbol=self.symbol,
            price_change_pct=change_pct,
            quote_volume=quote_volume,
            last_price=self.last_price,
            taker_buy_quote_volume=self.taker_buy_quote_volume,
            taker_sell_quote_volume=self.taker_sell_quote_volume,
            net_taker_quote_volume=self.taker_buy_quote_volume - self.taker_sell_quote_volume,
        )


@dataclass
class OrderBookAccumulator:
    symbol: str
    samples: int = 0
    bid_quote_1pct: float = 0.0
    ask_quote_1pct: float = 0.0
    imbalance_pct: float = 0.0
    nearest_buy_wall_price: float | None = None
    nearest_buy_wall_quote: float = 0.0
    nearest_sell_wall_price: float | None = None
    nearest_sell_wall_quote: float = 0.0

    def add(self, pressure: OrderBookPressure) -> None:
        self.samples += 1
        self.bid_quote_1pct += pressure.bid_quote_1pct
        self.ask_quote_1pct += pressure.ask_quote_1pct
        self.imbalance_pct += pressure.imbalance_pct
        if pressure.nearest_buy_wall_quote > self.nearest_buy_wall_quote:
            self.nearest_buy_wall_quote = pressure.nearest_buy_wall_quote
            self.nearest_buy_wall_price = pressure.nearest_buy_wall_price
        if pressure.nearest_sell_wall_quote > self.nearest_sell_wall_quote:
            self.nearest_sell_wall_quote = pressure.nearest_sell_wall_quote
            self.nearest_sell_wall_price = pressure.nearest_sell_wall_price

    def to_order_book_pressure(self) -> OrderBookPressure | None:
        if self.samples <= 0:
            return None
        return OrderBookPressure(
            symbol=self.symbol,
            bid_quote_1pct=self.bid_quote_1pct / self.samples,
            ask_quote_1pct=self.ask_quote_1pct / self.samples,
            imbalance_pct=self.imbalance_pct / self.samples,
            nearest_buy_wall_price=self.nearest_buy_wall_price,
            nearest_buy_wall_quote=self.nearest_buy_wall_quote,
            nearest_sell_wall_price=self.nearest_sell_wall_price,
            nearest_sell_wall_quote=self.nearest_sell_wall_quote,
        )


@dataclass(frozen=True)
class MarketScan:
    flow_stats: dict[str, MarketStat]
    order_books: dict[str, OrderBookPressure]
    samples: int


def load_env(path: str = ".env") -> None:
    env_path = Path(path)
    if not env_path.exists():
        return

    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[
            logging.FileHandler("bot.log", encoding="utf-8"),
            logging.StreamHandler(),
        ],
    )


def create_report(scan: MarketScan | None = None) -> str:
    analyses = []
    btc_unavailable = False
    market_stats = load_market_stats()
    flow_stats = load_flow_stats()
    if scan and scan.flow_stats:
        flow_stats.update(scan.flow_stats)
    confirm_stats = load_confirm_stats()
    symbols = select_symbols(flow_stats)
    order_books = {
        symbol: pressure
        for symbol, pressure in (scan.order_books.items() if scan else [])
        if symbol in symbols
    }
    missing_order_books = tuple(symbol for symbol in symbols if symbol not in order_books)
    if missing_order_books:
        order_books.update(load_order_books(missing_order_books))
    onchain_flows = load_onchain_flows(symbols)
    stablecoin_reserve = load_stablecoin_reserve()
    logging.info("Selected symbols: %s", ", ".join(symbols))

    for symbol in symbols:
        try:
            candles_by_timeframe = {
                timeframe: fetch_binance_klines(symbol, timeframe, limit=250)
                for timeframe in TIMEFRAMES
            }
            analyses.append(analyze_symbol(symbol, candles_by_timeframe))
        except Exception:
            logging.exception("Skipping %s because data fetch or analysis failed", symbol)
            if symbol == "BTCUSDT":
                btc_unavailable = True
            continue
    if not analyses:
        raise RuntimeError("Hicbir sembol icin rapor olusturulamadi")
    return build_report(
        analyses,
        costs=load_costs(),
        sectors=SECTORS,
        btc_unavailable=btc_unavailable,
        market_stats=market_stats,
        flow_stats=flow_stats,
        confirm_stats=confirm_stats,
        order_books=order_books,
        onchain_flows=onchain_flows,
        stablecoin_reserve=stablecoin_reserve,
    )


def load_market_stats() -> dict[str, MarketStat]:
    try:
        return fetch_24hr_stats(ALL_SYMBOLS)
    except Exception:
        logging.exception("Could not fetch 24hr market stats")
        return {}


def load_flow_stats() -> dict[str, MarketStat]:
    try:
        return fetch_recent_flow_stats(ALL_SYMBOLS, interval="5m")
    except Exception:
        logging.exception("Could not fetch 5m flow stats")
        return {}


def load_confirm_stats() -> dict[str, MarketStat]:
    try:
        return fetch_recent_flow_stats(ALL_SYMBOLS, interval="1h")
    except Exception:
        logging.exception("Could not fetch confirmation stats")
        return {}


def load_order_books(symbols: tuple[str, ...]) -> dict[str, OrderBookPressure]:
    try:
        return fetch_order_book_pressure(symbols)
    except Exception:
        logging.exception("Could not fetch order book pressure")
        return {}


def scan_market(symbols: tuple[str, ...], duration_seconds: int, scan_interval: float) -> MarketScan | None:
    if duration_seconds <= 0:
        return None

    interval = max(scan_interval, 1.0)
    flow_accumulators = {symbol: FlowAccumulator(symbol) for symbol in symbols}
    order_book_accumulators = {symbol: OrderBookAccumulator(symbol) for symbol in symbols}
    sample_count = 0
    started_at = time.monotonic()
    last_trade_end_ms = int(time.time() * 1000) - int(interval * 1000)
    logging.info("Pre-scanning market for %ss every %.1fs", duration_seconds, interval)

    while time.monotonic() - started_at < duration_seconds:
        sample_started = time.monotonic()
        trade_end_ms = int(time.time() * 1000)
        trade_start_ms = max(last_trade_end_ms + 1, trade_end_ms - int((interval + 2) * 1000))

        with ThreadPoolExecutor(max_workers=2) as executor:
            order_book_future = executor.submit(fetch_order_book_pressure, symbols)
            trade_flow_future = executor.submit(fetch_recent_trade_flow, symbols, trade_start_ms, trade_end_ms)
            order_books = order_book_future.result()
            trade_stats = trade_flow_future.result()
        last_trade_end_ms = trade_end_ms

        for symbol, pressure in order_books.items():
            order_book_accumulators[symbol].add(pressure)
        for symbol, stat in trade_stats.items():
            flow_accumulators[symbol].add(stat)

        sample_count += 1
        if sample_count == 1 or sample_count % 30 == 0:
            logging.info("Market pre-scan samples: %s", sample_count)

        elapsed = time.monotonic() - sample_started
        sleep_seconds = interval - elapsed
        if sleep_seconds > 0:
            time.sleep(sleep_seconds)

    flow_stats = {
        symbol: stat
        for symbol, accumulator in flow_accumulators.items()
        if (stat := accumulator.to_market_stat())
    }
    order_books = {
        symbol: pressure
        for symbol, accumulator in order_book_accumulators.items()
        if (pressure := accumulator.to_order_book_pressure())
    }
    logging.info("Market pre-scan completed with %s samples", sample_count)
    return MarketScan(flow_stats=flow_stats, order_books=order_books, samples=sample_count)


def load_onchain_flows(symbols: tuple[str, ...]) -> dict[str, OnChainFlow]:
    try:
        return fetch_onchain_flows(symbols)
    except Exception:
        logging.exception("Could not fetch on-chain flows")
        return {}


def load_stablecoin_reserve() -> StablecoinReserve | None:
    try:
        return fetch_stablecoin_reserve()
    except Exception:
        logging.exception("Could not fetch stablecoin reserve")
        return None


def select_symbols(stats: dict[str, MarketStat]) -> tuple[str, ...]:
    ranked: list[tuple[float, str]] = []
    for symbol in ALT_CANDIDATES:
        stat = stats.get(symbol)
        if not stat or stat.quote_volume < MIN_ALT_5M_QUOTE_VOLUME:
            continue
        # 5M positive change with USDT volume is the near-real-time money-flow proxy.
        flow_score = stat.quote_volume * max(stat.price_change_pct, 0.05)
        ranked.append((flow_score, symbol))

    top_alts = [symbol for _, symbol in sorted(ranked, reverse=True)[:MAX_ALT_SYMBOLS]]
    if not top_alts:
        top_alts = list(ALT_CANDIDATES[:MAX_ALT_SYMBOLS])
    return CORE_SYMBOLS + tuple(top_alts)


def load_costs() -> dict[str, float]:
    costs: dict[str, float] = {}
    for symbol in ALL_SYMBOLS:
        value = os.getenv(f"{symbol}_COST") or os.getenv(f"COST_{symbol}")
        if not value:
            continue
        try:
            costs[symbol] = float(value)
        except ValueError:
            logging.warning("Invalid cost value for %s: %s", symbol, value)
    return costs


def run_once(
    send_to_telegram: bool = True,
    pre_scan_seconds: int = 0,
    scan_interval: float = 1.0,
) -> str:
    scan = scan_market(ALL_SYMBOLS, pre_scan_seconds, scan_interval)
    report = create_report(scan=scan)
    logging.info("Report created")

    if send_to_telegram:
        token = "".join((os.getenv("TELEGRAM_BOT_TOKEN") or "").split())
        chat_id = "".join((os.getenv("TELEGRAM_CHAT_ID") or "").split())
        if not token or not chat_id:
            raise RuntimeError("TELEGRAM_BOT_TOKEN ve TELEGRAM_CHAT_ID .env icinde olmali")
        send_telegram_message(token, chat_id, report)

    return report


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Crypto market analysis Telegram bot")
    parser.add_argument("--once", action="store_true", help="Run once and exit")
    parser.add_argument("--no-telegram", action="store_true", help="Print only, do not send Telegram")
    parser.add_argument("--interval", type=int, default=300, help="Loop interval in seconds")
    parser.add_argument(
        "--pre-scan-seconds",
        type=int,
        default=int(os.getenv("PRE_SCAN_SECONDS", "0")),
        help="Collect order book and trade-flow samples before sending the report",
    )
    parser.add_argument(
        "--scan-interval",
        type=float,
        default=float(os.getenv("SCAN_INTERVAL_SECONDS", "1")),
        help="Seconds between market pre-scan samples",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    setup_logging()
    load_env()
    had_error = False

    while True:
        try:
            report = run_once(
                send_to_telegram=not args.no_telegram,
                pre_scan_seconds=args.pre_scan_seconds,
                scan_interval=args.scan_interval,
            )
            try:
                print(report, flush=True)
            except BrokenPipeError:
                logging.warning("Stdout closed before report could be printed")
        except Exception:
            had_error = True
            logging.exception("Bot run failed")

        if args.once:
            return 1 if had_error else 0
        time.sleep(args.interval)


if __name__ == "__main__":
    raise SystemExit(main())
