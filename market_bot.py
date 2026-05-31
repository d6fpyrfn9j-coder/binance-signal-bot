#!/usr/bin/env python3
"""
Binance market direction bot.

Fetches Binance candles for 1h, 4h and 1d, scores market direction with
trend/momentum/volume indicators, and optionally includes ETF flow data.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any


BINANCE_BASE_URL = "https://api.binance.com"
SOSO_URL = "https://api.sosovalue.xyz/openapi/v2/etf/historicalInflowChart"
COINGECKO_BASE_URL = "https://api.coingecko.com/api/v3"
DEFILLAMA_STABLECOINS_URL = "https://stablecoins.llama.fi/stablecoincharts/all"


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
class TimeframeSignal:
    interval: str
    score: float
    direction: str
    close: float
    change_pct: float
    rsi: float
    ema20: float
    ema50: float
    macd_hist: float
    volume_ratio: float
    reasons: list[str]


def http_json(
    url: str,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    payload: dict[str, Any] | None = None,
    timeout: int = 15,
) -> Any:
    body = None
    request_headers = {"User-Agent": "market-direction-bot/1.0"}
    if headers:
        request_headers.update(headers)
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
        request_headers["Content-Type"] = "application/json"

    req = urllib.request.Request(url, data=body, headers=request_headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            raw = response.read().decode("utf-8")
            return json.loads(raw)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code} from {url}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Network error while calling {url}: {exc.reason}") from exc


def fetch_klines(symbol: str, interval: str, limit: int) -> list[Candle]:
    url = f"{BINANCE_BASE_URL}/api/v3/klines?symbol={symbol}&interval={interval}&limit={limit}"
    rows = http_json(url)
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


def fetch_optional_json(url: str) -> Any | None:
    try:
        return http_json(url)
    except RuntimeError:
        return None


def sma(values: list[float], period: int) -> float:
    if len(values) < period:
        raise ValueError(f"Need at least {period} values")
    return sum(values[-period:]) / period


def ema(values: list[float], period: int) -> float:
    if len(values) < period:
        raise ValueError(f"Need at least {period} values")
    multiplier = 2 / (period + 1)
    current = sum(values[:period]) / period
    for value in values[period:]:
        current = (value - current) * multiplier + current
    return current


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


def macd_histogram(values: list[float]) -> float:
    if len(values) < 35:
        raise ValueError("Need at least 35 values for MACD")
    ema12_series = ema_series(values, 12)
    ema26_series = ema_series(values, 26)
    offset = len(ema12_series) - len(ema26_series)
    macd_line = [ema12_series[i + offset] - ema26_series[i] for i in range(len(ema26_series))]
    signal_line = ema_series(macd_line, 9)
    return macd_line[-1] - signal_line[-1]


def ema_series(values: list[float], period: int) -> list[float]:
    if len(values) < period:
        raise ValueError(f"Need at least {period} values")
    result: list[float] = []
    multiplier = 2 / (period + 1)
    current = sum(values[:period]) / period
    result.append(current)
    for value in values[period:]:
        current = (value - current) * multiplier + current
        result.append(current)
    return result


def classify(score: float) -> str:
    if score >= 3.0:
        return "GUCLU YUKARI"
    if score >= 1.0:
        return "YUKARI"
    if score <= -3.0:
        return "GUCLU ASAGI"
    if score <= -1.0:
        return "ASAGI"
    return "NOTR"


def analyze_timeframe(interval: str, candles: list[Candle]) -> TimeframeSignal:
    closes = [c.close for c in candles]
    volumes = [c.volume for c in candles]
    last = candles[-1]
    previous = candles[-2]
    last_rsi = rsi(closes)
    last_ema20 = ema(closes, 20)
    last_ema50 = ema(closes, 50)
    last_macd = macd_histogram(closes)
    volume_ratio = last.volume / sma(volumes, 20)
    change_pct = ((last.close - previous.close) / previous.close) * 100

    score = 0.0
    reasons: list[str] = []

    if last.close > last_ema20:
        score += 0.7
        reasons.append("fiyat EMA20 ustunde")
    else:
        score -= 0.7
        reasons.append("fiyat EMA20 altinda")

    if last.close > last_ema50:
        score += 0.8
        reasons.append("fiyat EMA50 ustunde")
    else:
        score -= 0.8
        reasons.append("fiyat EMA50 altinda")

    if last_ema20 > last_ema50:
        score += 0.9
        reasons.append("EMA20 > EMA50")
    else:
        score -= 0.9
        reasons.append("EMA20 < EMA50")

    if 50 <= last_rsi < 70:
        score += 0.7
        reasons.append("RSI pozitif bolgede")
    elif last_rsi >= 70:
        score += 0.2
        reasons.append("RSI asiri alim bolgesine yakin")
    elif 30 < last_rsi < 50:
        score -= 0.7
        reasons.append("RSI zayif bolgede")
    else:
        score -= 0.2
        reasons.append("RSI asiri satim bolgesine yakin")

    if last_macd > 0:
        score += 0.7
        reasons.append("MACD histogram pozitif")
    else:
        score -= 0.7
        reasons.append("MACD histogram negatif")

    if volume_ratio >= 1.25 and change_pct > 0:
        score += 0.7
        reasons.append("yukari hareket hacimle destekli")
    elif volume_ratio >= 1.25 and change_pct < 0:
        score -= 0.7
        reasons.append("dususte hacim artisi var")
    elif volume_ratio < 0.75:
        score *= 0.85
        reasons.append("hacim dusuk, sinyal zayif")
    else:
        reasons.append("hacim ortalamaya yakin")

    if change_pct > 0.8:
        score += 0.3
    elif change_pct < -0.8:
        score -= 0.3

    return TimeframeSignal(
        interval=interval,
        score=score,
        direction=classify(score),
        close=last.close,
        change_pct=change_pct,
        rsi=last_rsi,
        ema20=last_ema20,
        ema50=last_ema50,
        macd_hist=last_macd,
        volume_ratio=volume_ratio,
        reasons=reasons,
    )


def fetch_etf_flow(etf_type: str, days: int = 5) -> dict[str, Any] | None:
    api_key = os.getenv("SOSO_API_KEY")
    if not api_key:
        return None
    data = http_json(
        SOSO_URL,
        method="POST",
        headers={"x-soso-api-key": api_key},
        payload={"type": etf_type},
    )
    if data.get("code") != 0:
        raise RuntimeError(f"SoSoValue API error: {data}")
    items = data.get("data", {}).get("list", [])
    if not items:
        return None
    latest = items[-1]
    recent = items[-days:]
    total_5d = sum(float(item.get("totalNetInflow", 0) or 0) for item in recent)
    return {
        "date": latest.get("date"),
        "latest_flow": float(latest.get("totalNetInflow", 0) or 0),
        "total_period": total_5d,
    }


def etf_score(flow: dict[str, Any] | None) -> tuple[float, str]:
    if not flow:
        return 0.0, "ETF verisi yok"
    latest = flow["latest_flow"]
    total_5d = flow["total_period"]
    score = 0.0
    if latest > 0:
        score += 0.6
    elif latest < 0:
        score -= 0.6
    if total_5d > 0:
        score += 0.6
    elif total_5d < 0:
        score -= 0.6
    return score, f"ETF son gun {money(latest)}, 5 gun toplam {money(total_5d)}"


def format_pct(value: float | None) -> str:
    if value is None:
        return "yok"
    sign = "+" if value > 0 else ""
    return f"{sign}{value:.1f}%"


def format_dominance(current: float | None, change: float | None) -> str:
    if change is not None:
        return format_pct(change)
    if current is not None:
        return f"{current:.1f}%"
    return "yok"


def compact_money(value: float | None) -> str:
    if value is None:
        return "yok"
    sign = "+" if value > 0 else ""
    abs_value = abs(value)
    if abs_value >= 1_000_000_000:
        return f"{sign}{value / 1_000_000_000:.1f} milyar $"
    if abs_value >= 1_000_000:
        return f"{sign}{value / 1_000_000:.0f}M $"
    return f"{sign}{value:,.0f} $"


def get_nested_number(item: dict[str, Any], *keys: str) -> float | None:
    current: Any = item
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    if current is None:
        return None
    return float(current)


def fetch_btc_dominance_current() -> float | None:
    data = fetch_optional_json(f"{COINGECKO_BASE_URL}/global")
    if not data:
        return None
    value = data.get("data", {}).get("market_cap_percentage", {}).get("btc")
    if value is None:
        return None
    return float(value)


def fetch_btc_dominance_change(days: int = 7) -> float | None:
    btc_url = f"{COINGECKO_BASE_URL}/coins/bitcoin/market_chart?vs_currency=usd&days={days + 1}"
    global_url = f"{COINGECKO_BASE_URL}/global/market_cap_chart?vs_currency=usd&days={days + 1}"
    btc_data = fetch_optional_json(btc_url)
    global_data = fetch_optional_json(global_url)
    if not btc_data or not global_data:
        return None

    btc_caps = btc_data.get("market_caps", [])
    total_caps = global_data.get("market_cap_chart", {}).get("market_cap", [])
    if not total_caps:
        total_caps = global_data.get("market_caps", [])
    if len(btc_caps) < 2 or len(total_caps) < 2:
        return None

    start_dom = (float(btc_caps[0][1]) / float(total_caps[0][1])) * 100
    end_dom = (float(btc_caps[-1][1]) / float(total_caps[-1][1])) * 100
    return end_dom - start_dom


def fetch_ethbtc_change(days: int = 7) -> float | None:
    candles = fetch_klines("ETHBTC", "1d", days + 1)
    if len(candles) < 2:
        return None
    return ((candles[-1].close - candles[0].close) / candles[0].close) * 100


def fetch_etf_period_total(etf_type: str, days: int = 7) -> float | None:
    flow = fetch_etf_flow(etf_type, days)
    if not flow:
        return None
    return float(flow["total_period"])


def fetch_stablecoin_change(days: int = 7) -> float | None:
    data = fetch_optional_json(DEFILLAMA_STABLECOINS_URL)
    if not isinstance(data, list) or len(data) < days + 1:
        return None

    start = get_nested_number(data[-days - 1], "totalCirculatingUSD", "peggedUSD")
    end = get_nested_number(data[-1], "totalCirculatingUSD", "peggedUSD")
    if start is None or end is None:
        return None
    return end - start


def build_flow_comment(
    btc_d_current: float | None,
    btc_d_change: float | None,
    ethbtc_change: float | None,
    btc_etf: float | None,
    eth_etf: float | None,
    stable_change: float | None,
) -> list[str]:
    comments: list[str] = []
    capital_inflow = (stable_change or 0) > 0 or (btc_etf or 0) + (eth_etf or 0) > 0
    if capital_inflow:
        comments.append("Piyasaya yeni sermaye girisi var.")
    else:
        comments.append("Yeni sermaye girisi zayif.")

    btc_lead = (
        ((btc_d_change or 0) > 0 and (ethbtc_change or 0) < 0)
        or (btc_d_change is None and (ethbtc_change or 0) < 0 and (btc_d_current or 0) >= 50)
    )
    alt_lead = (
        ((btc_d_change or 0) < 0 and (ethbtc_change or 0) > 0)
        or (btc_d_change is None and (ethbtc_change or 0) > 0 and (btc_d_current or 100) < 55)
    )

    if btc_lead:
        comments.append("Para agirlikli olarak Bitcoin'e yoneliyor.")
    elif alt_lead:
        comments.append("Bitcoin disina para gecisi gucleniyor.")
    else:
        comments.append("Sermaye dagilimi net degil.")

    if (btc_d_change or 0) < -0.5 and (ethbtc_change or 0) > 1.0:
        comments.append("Altcoin sezonu icin erken pozitif sinyal var.")
    else:
        comments.append("Altcoin sezonu sinyali henuz gorunmuyor.")
    return comments


def build_flow_report() -> str:
    btc_d_current = fetch_btc_dominance_current()
    btc_d_change = fetch_btc_dominance_change()
    ethbtc_change = fetch_ethbtc_change()
    btc_etf = fetch_etf_period_total("us-btc-spot")
    eth_etf = fetch_etf_period_total("us-eth-spot")
    stable_change = fetch_stablecoin_change()
    comments = build_flow_comment(
        btc_d_current,
        btc_d_change,
        ethbtc_change,
        btc_etf,
        eth_etf,
        stable_change,
    )

    return "\n".join(
        [
            "PIYASA AKIS RAPORU",
            "",
            f"BTC Dominansi: {format_dominance(btc_d_current, btc_d_change)}",
            f"ETH/BTC: {format_pct(ethbtc_change)}",
            "",
            "Son 7 gun:",
            f"BTC ETF girisleri: {compact_money(btc_etf)}",
            f"ETH ETF girisleri: {compact_money(eth_etf)}",
            "",
            "Stablecoin Market Cap:",
            compact_money(stable_change),
            "",
            "Yorum:",
            *comments,
        ]
    )


def send_telegram(text: str) -> None:
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        raise RuntimeError("Telegram icin TELEGRAM_BOT_TOKEN ve TELEGRAM_CHAT_ID gerekli")
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    http_json(
        url,
        method="POST",
        payload={
            "chat_id": chat_id,
            "text": text,
            "disable_web_page_preview": True,
        },
    )


def money(value: float) -> str:
    sign = "+" if value > 0 else ""
    abs_value = abs(value)
    if abs_value >= 1_000_000_000:
        return f"{sign}{value / 1_000_000_000:.2f}B USD"
    if abs_value >= 1_000_000:
        return f"{sign}{value / 1_000_000:.2f}M USD"
    return f"{sign}{value:,.0f} USD"


def build_report(symbol: str, etf_type: str | None, limit: int) -> str:
    weights = {"1h": 0.25, "4h": 0.35, "1d": 0.40}
    signals: list[TimeframeSignal] = []
    for interval in ("1h", "4h", "1d"):
        signals.append(analyze_timeframe(interval, fetch_klines(symbol, interval, limit)))

    weighted_score = sum(signal.score * weights[signal.interval] for signal in signals)
    flow = fetch_etf_flow(etf_type) if etf_type else None
    flow_score, flow_text = etf_score(flow)
    final_score = weighted_score + flow_score
    final_direction = classify(final_score)
    now = dt.datetime.now(dt.timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")

    lines = [
        f"{symbol} | {final_direction} | skor {final_score:.2f}",
        f"1h {signals[0].direction} ({signals[0].change_pct:+.2f}%, RSI {signals[0].rsi:.0f}, Vx{signals[0].volume_ratio:.1f})",
        f"4h {signals[1].direction} ({signals[1].change_pct:+.2f}%, RSI {signals[1].rsi:.0f}, Vx{signals[1].volume_ratio:.1f})",
        f"1d {signals[2].direction} ({signals[2].change_pct:+.2f}%, RSI {signals[2].rsi:.0f}, Vx{signals[2].volume_ratio:.1f})",
        f"ETF: {flow_text}",
        f"{now}",
    ]
    return "\n".join(lines)


def build_detailed_report(symbol: str, etf_type: str | None, limit: int) -> str:
    weights = {"1h": 0.25, "4h": 0.35, "1d": 0.40}
    signals: list[TimeframeSignal] = []
    for interval in ("1h", "4h", "1d"):
        signals.append(analyze_timeframe(interval, fetch_klines(symbol, interval, limit)))

    weighted_score = sum(signal.score * weights[signal.interval] for signal in signals)
    flow = fetch_etf_flow(etf_type) if etf_type else None
    flow_score, flow_text = etf_score(flow)
    final_score = weighted_score + flow_score
    final_direction = classify(final_score)
    now = dt.datetime.now(dt.timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")

    lines = [
        f"Piyasa yonu: {final_direction}",
        f"Sembol: {symbol}",
        f"Skor: {final_score:.2f} (mumlar {weighted_score:.2f}, ETF {flow_score:.2f})",
        f"Zaman: {now}",
        "",
        "Zaman dilimleri:",
    ]
    for signal in signals:
        lines.extend(
            [
                f"- {signal.interval}: {signal.direction} | skor {signal.score:.2f}",
                f"  fiyat {signal.close:.4f}, degisim {signal.change_pct:+.2f}%, RSI {signal.rsi:.1f}, hacim x{signal.volume_ratio:.2f}",
                f"  nedenler: {', '.join(signal.reasons[:4])}",
            ]
        )
    lines.extend(["", f"ETF: {flow_text}"])
    return "\n".join(lines)


def load_config(path: str | None) -> dict[str, Any]:
    if not path:
        return {}
    with open(path, "r", encoding="utf-8") as file:
        return json.load(file)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Binance market direction bot")
    parser.add_argument("--config", help="JSON config path")
    parser.add_argument("--symbol", help="Binance symbol, example BTCUSDT")
    parser.add_argument("--etf", help="ETF type, example us-btc-spot or us-eth-spot")
    parser.add_argument("--watch", action="store_true", help="Run continuously")
    parser.add_argument("--telegram", action="store_true", help="Send report to Telegram")
    parser.add_argument("--details", action="store_true", help="Print detailed report")
    parser.add_argument("--flow", action="store_true", help="Print market flow report")
    parser.add_argument("--interval", type=int, help="Watch interval in seconds")
    parser.add_argument("--klines-limit", type=int, help="Number of candles to fetch")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    config = load_config(args.config)
    symbol = (args.symbol or config.get("symbol") or "BTCUSDT").upper()
    etf_type = args.etf if args.etf is not None else config.get("etf")
    watch = bool(args.watch or config.get("watch", False))
    telegram = bool(args.telegram or config.get("telegram", False))
    details = bool(args.details or config.get("details", False))
    flow = bool(args.flow or config.get("flow", False))
    interval = int(args.interval or config.get("interval", 300))
    limit = int(args.klines_limit or config.get("klines_limit", 220))

    while True:
        try:
            if flow:
                report = build_flow_report()
            elif details:
                report = build_detailed_report(symbol, etf_type, limit)
            else:
                report = build_report(symbol, etf_type, limit)
            print(report, flush=True)
            if telegram:
                send_telegram(report)
        except Exception as exc:
            print(f"Hata: {exc}", file=sys.stderr, flush=True)
            if not watch:
                return 1
        if not watch:
            return 0
        print("\n---\n", flush=True)
        time.sleep(interval)


if __name__ == "__main__":
    raise SystemExit(main())
