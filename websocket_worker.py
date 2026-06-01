#!/usr/bin/env python3
"""Continuous Binance WebSocket worker for Telegram market reports."""

from __future__ import annotations

import argparse
import json
import logging
import os
import threading
import time
from dataclasses import dataclass

import websocket

from data_fetcher import MarketStat, OrderBookPressure
from main import (
    ALL_SYMBOLS,
    FlowAccumulator,
    MarketScan,
    OrderBookAccumulator,
    create_report,
    load_env,
    setup_logging,
)
from telegram_sender import send_telegram_message


BINANCE_STREAM_BASE_URL = "wss://stream.binance.com:9443/stream?streams="


@dataclass(frozen=True)
class StreamStats:
    trade_messages: int
    depth_messages: int
    started_at: float
    last_message_at: float | None


def _wall(levels: list[tuple[float, float]]) -> tuple[float | None, float]:
    if not levels:
        return None, 0.0
    quotes = [(price, price * quantity) for price, quantity in levels]
    avg_quote = sum(quote for _, quote in quotes) / len(quotes)
    price, quote = max(quotes, key=lambda item: item[1])
    if avg_quote <= 0 or quote < avg_quote * 3:
        return None, 0.0
    return price, quote


def _order_book_pressure_from_depth(symbol: str, data: dict) -> OrderBookPressure | None:
    bids = [(float(price), float(quantity)) for price, quantity in data.get("bids", [])]
    asks = [(float(price), float(quantity)) for price, quantity in data.get("asks", [])]
    if not bids or not asks:
        return None

    bid_quote = sum(price * quantity for price, quantity in bids)
    ask_quote = sum(price * quantity for price, quantity in asks)
    total_quote = bid_quote + ask_quote
    imbalance_pct = ((bid_quote - ask_quote) / total_quote) * 100 if total_quote else 0.0
    buy_wall_price, buy_wall_quote = _wall(bids)
    sell_wall_price, sell_wall_quote = _wall(asks)

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


class StreamState:
    def __init__(self, symbols: tuple[str, ...]) -> None:
        self.symbols = symbols
        self._lock = threading.Lock()
        self._reset_locked()
        self.started_at = time.time()
        self.last_message_at: float | None = None
        self.trade_messages = 0
        self.depth_messages = 0

    def _reset_locked(self) -> None:
        self.flow_accumulators = {symbol: FlowAccumulator(symbol) for symbol in self.symbols}
        self.order_book_accumulators = {
            symbol: OrderBookAccumulator(symbol) for symbol in self.symbols
        }

    def add_trade(self, symbol: str, data: dict) -> None:
        price = float(data.get("p", 0) or 0)
        quantity = float(data.get("q", 0) or 0)
        quote = price * quantity
        if quote <= 0:
            return

        if data.get("m"):
            buy_quote = 0.0
            sell_quote = quote
        else:
            buy_quote = quote
            sell_quote = 0.0

        stat = MarketStat(
            symbol=symbol,
            price_change_pct=0.0,
            quote_volume=quote,
            last_price=price,
            taker_buy_quote_volume=buy_quote,
            taker_sell_quote_volume=sell_quote,
            net_taker_quote_volume=buy_quote - sell_quote,
        )
        with self._lock:
            self.flow_accumulators[symbol].add(stat)
            self.trade_messages += 1
            self.last_message_at = time.time()

    def add_depth(self, symbol: str, data: dict) -> None:
        pressure = _order_book_pressure_from_depth(symbol, data)
        if not pressure:
            return
        with self._lock:
            self.order_book_accumulators[symbol].add(pressure)
            self.depth_messages += 1
            self.last_message_at = time.time()

    def snapshot_and_reset(self) -> tuple[MarketScan, StreamStats]:
        with self._lock:
            samples = sum(
                accumulator.samples for accumulator in self.order_book_accumulators.values()
            )
            flow_stats = {
                symbol: stat
                for symbol, accumulator in self.flow_accumulators.items()
                if (stat := accumulator.to_market_stat())
            }
            order_books = {
                symbol: pressure
                for symbol, accumulator in self.order_book_accumulators.items()
                if (pressure := accumulator.to_order_book_pressure())
            }
            stats = StreamStats(
                trade_messages=self.trade_messages,
                depth_messages=self.depth_messages,
                started_at=self.started_at,
                last_message_at=self.last_message_at,
            )
            self._reset_locked()
            self.trade_messages = 0
            self.depth_messages = 0
            self.started_at = time.time()
        return MarketScan(flow_stats=flow_stats, order_books=order_books, samples=samples), stats


def _stream_symbol(stream_name: str) -> str:
    return stream_name.split("@", 1)[0].upper()


def _handle_message(state: StreamState, raw_message: str) -> None:
    payload = json.loads(raw_message)
    stream_name = payload.get("stream", "")
    data = payload.get("data", {})
    if not stream_name or not data:
        return

    symbol = _stream_symbol(stream_name)
    if stream_name.endswith("@aggTrade"):
        state.add_trade(symbol, data)
    elif "@depth20" in stream_name:
        state.add_depth(symbol, data)


def _combined_stream_url(symbols: tuple[str, ...]) -> str:
    streams: list[str] = []
    for symbol in symbols:
        lower_symbol = symbol.lower()
        streams.append(f"{lower_symbol}@aggTrade")
        streams.append(f"{lower_symbol}@depth20@1000ms")
    return BINANCE_STREAM_BASE_URL + "/".join(streams)


def run_stream_loop(state: StreamState, symbols: tuple[str, ...], reconnect_delay: int) -> None:
    url = _combined_stream_url(symbols)

    def on_open(_: websocket.WebSocketApp) -> None:
        logging.info("Binance WebSocket connected with %s streams", len(symbols) * 2)

    def on_message(_: websocket.WebSocketApp, message: str) -> None:
        try:
            _handle_message(state, message)
        except Exception:
            logging.exception("Could not process WebSocket message")

    def on_error(_: websocket.WebSocketApp, error: Exception) -> None:
        logging.warning("Binance WebSocket error: %s", error)

    def on_close(_: websocket.WebSocketApp, status_code: int, message: str) -> None:
        logging.warning("Binance WebSocket closed: %s %s", status_code, message)

    while True:
        app = websocket.WebSocketApp(
            url,
            on_open=on_open,
            on_message=on_message,
            on_error=on_error,
            on_close=on_close,
        )
        app.run_forever(ping_interval=20, ping_timeout=10)
        logging.info("Reconnecting Binance WebSocket in %ss", reconnect_delay)
        time.sleep(reconnect_delay)


def send_report_from_stream(state: StreamState) -> str:
    scan, stats = state.snapshot_and_reset()
    logging.info(
        "Creating report from WebSocket window: trades=%s depth=%s",
        stats.trade_messages,
        stats.depth_messages,
    )
    report = create_report(scan=scan)
    token = "".join((os.getenv("TELEGRAM_BOT_TOKEN") or "").split())
    chat_id = "".join((os.getenv("TELEGRAM_CHAT_ID") or "").split())
    if not token or not chat_id:
        raise RuntimeError("TELEGRAM_BOT_TOKEN ve TELEGRAM_CHAT_ID ayarlanmali")
    send_telegram_message(token, chat_id, report)
    return report


def run_report_loop(state: StreamState, report_interval: int, warmup_seconds: int) -> None:
    logging.info("Worker warmup: %ss", warmup_seconds)
    time.sleep(max(warmup_seconds, 0))
    while True:
        started_at = time.monotonic()
        try:
            report = send_report_from_stream(state)
            print(report, flush=True)
        except Exception:
            logging.exception("WebSocket worker report failed")

        elapsed = time.monotonic() - started_at
        time.sleep(max(report_interval - elapsed, 5))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Continuous Binance WebSocket Telegram worker")
    parser.add_argument(
        "--report-interval",
        type=int,
        default=int(os.getenv("REPORT_INTERVAL_SECONDS", "300")),
        help="Seconds between Telegram reports",
    )
    parser.add_argument(
        "--warmup-seconds",
        type=int,
        default=int(os.getenv("WORKER_WARMUP_SECONDS", "300")),
        help="Seconds to collect WebSocket data before first report",
    )
    parser.add_argument(
        "--reconnect-delay",
        type=int,
        default=int(os.getenv("WEBSOCKET_RECONNECT_DELAY", "5")),
        help="Seconds to wait before reconnecting WebSocket",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    setup_logging()
    load_env()
    state = StreamState(ALL_SYMBOLS)
    stream_thread = threading.Thread(
        target=run_stream_loop,
        args=(state, ALL_SYMBOLS, args.reconnect_delay),
        daemon=True,
    )
    stream_thread.start()
    run_report_loop(state, args.report_interval, args.warmup_seconds)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
