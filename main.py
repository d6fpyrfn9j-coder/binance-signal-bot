#!/usr/bin/env python3
"""Hourly crypto market analysis bot."""

from __future__ import annotations

import argparse
import logging
import os
import time
from pathlib import Path

from analyzer import analyze_symbol, build_report
from data_fetcher import MarketStat, fetch_24hr_stats, fetch_binance_klines, fetch_recent_flow_stats
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


def create_report() -> str:
    analyses = []
    btc_unavailable = False
    market_stats = load_market_stats()
    flow_stats = load_flow_stats()
    confirm_stats = load_confirm_stats()
    symbols = select_symbols(flow_stats)
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
        logging.exception("Could not fetch 1h confirmation stats")
        return {}


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


def run_once(send_to_telegram: bool = True) -> str:
    report = create_report()
    logging.info("Report created")

    if send_to_telegram:
        token = os.getenv("TELEGRAM_BOT_TOKEN")
        chat_id = os.getenv("TELEGRAM_CHAT_ID")
        if not token or not chat_id:
            raise RuntimeError("TELEGRAM_BOT_TOKEN ve TELEGRAM_CHAT_ID .env icinde olmali")
        send_telegram_message(token, chat_id, report)

    return report


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Crypto market analysis Telegram bot")
    parser.add_argument("--once", action="store_true", help="Run once and exit")
    parser.add_argument("--no-telegram", action="store_true", help="Print only, do not send Telegram")
    parser.add_argument("--interval", type=int, default=300, help="Loop interval in seconds")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    setup_logging()
    load_env()
    had_error = False

    while True:
        try:
            report = run_once(send_to_telegram=not args.no_telegram)
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
