"""Optional market-wide context: BTC dominance and headline risk."""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass


COINGECKO_GLOBAL_URL = "https://api.coingecko.com/api/v3/global"
COINPAPRIKA_GLOBAL_URL = "https://api.coinpaprika.com/v1/global"
GOOGLE_NEWS_RSS_URL = "https://news.google.com/rss/search"
NEWS_CACHE_SECONDS = int(os.getenv("NEWS_CACHE_SECONDS", "900"))


@dataclass(frozen=True)
class NewsSignal:
    topic: str
    label: str
    impact: int
    headline: str | None = None


@dataclass(frozen=True)
class MarketContext:
    btc_dominance_pct: float | None
    news_signals: tuple[NewsSignal, ...]


_CONTEXT_CACHE: tuple[float, MarketContext] | None = None


def _read_url(url: str, timeout: int = 10) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "binance-signal-bot/1.0"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Network error: {exc.reason}") from exc


def _coingecko_btc_dominance() -> float | None:
    payload = json.loads(_read_url(COINGECKO_GLOBAL_URL, timeout=10).decode("utf-8"))
    btc_value = payload.get("data", {}).get("market_cap_percentage", {}).get("btc")
    return float(btc_value) if btc_value is not None else None


def _coinpaprika_btc_dominance() -> float | None:
    payload = json.loads(_read_url(COINPAPRIKA_GLOBAL_URL, timeout=10).decode("utf-8"))
    btc_value = payload.get("bitcoin_dominance_percentage")
    return float(btc_value) if btc_value is not None else None


def fetch_btc_dominance() -> float | None:
    errors: list[str] = []
    for name, fetcher in (
        ("coingecko", _coingecko_btc_dominance),
        ("coinpaprika", _coinpaprika_btc_dominance),
    ):
        try:
            value = fetcher()
            if value is not None:
                return value
            errors.append(f"{name}: empty")
        except Exception as exc:
            errors.append(f"{name}: {exc}")

    logging.warning("Could not fetch BTC dominance: %s", " | ".join(errors))
    return None


def _rss_titles(query: str, limit: int = 5) -> list[str]:
    params = urllib.parse.urlencode(
        {
            "q": query,
            "hl": "en-US",
            "gl": "US",
            "ceid": "US:en",
        }
    )
    raw_xml = _read_url(f"{GOOGLE_NEWS_RSS_URL}?{params}", timeout=10)
    root = ET.fromstring(raw_xml)
    titles: list[str] = []
    for item in root.findall(".//item")[:limit]:
        title = item.findtext("title")
        if title:
            titles.append(title.strip())
    return titles


def _headline_match(titles: list[str], keywords: tuple[str, ...]) -> str | None:
    for title in titles:
        lower_title = title.lower()
        if any(keyword in lower_title for keyword in keywords):
            return title
    return None


def _etf_signal(titles: list[str]) -> NewsSignal:
    outflow = _headline_match(titles, ("outflow", "outflows", "withdraw", "redemption"))
    inflow = _headline_match(titles, ("inflow", "inflows", "buying", "record flow"))
    if outflow:
        return NewsSignal("ETF", "çıkış riski 🔴", -4, outflow)
    if inflow:
        return NewsSignal("ETF", "giriş var 🟢", 4, inflow)
    return NewsSignal("ETF", "nötr 🟡", 0, None)


def _fed_signal(titles: list[str]) -> NewsSignal:
    hawkish = _headline_match(titles, ("rate hike", "hike", "hawkish", "higher for longer"))
    dovish = _headline_match(titles, ("rate cut", "cut rates", "dovish", "easing"))
    if hawkish:
        return NewsSignal("FED", "riskli 🔴", -5, hawkish)
    if dovish:
        return NewsSignal("FED", "destek 🟢", 4, dovish)
    return NewsSignal("FED", "sakin 🟢", 1, None)


def _risk_signal(titles: list[str]) -> NewsSignal:
    high_risk = _headline_match(
        titles,
        (
            "hack",
            "hacked",
            "exploit",
            "stolen",
            "lawsuit",
            "sues",
            "sec charges",
            "criminal",
            "bankruptcy",
        ),
    )
    if high_risk:
        return NewsSignal("Haber", "hack/dava riski 🔴", -6, high_risk)
    return NewsSignal("Haber", "büyük risk yok 🟢", 2, None)


def fetch_news_signals() -> tuple[NewsSignal, ...]:
    if os.getenv("NEWS_FILTER_ENABLED", "true").lower() in {"0", "false", "no"}:
        return ()

    try:
        etf_titles = _rss_titles("bitcoin ethereum ETF inflow outflow crypto when:1d", limit=6)
        fed_titles = _rss_titles("Federal Reserve rate cut hike crypto market when:1d", limit=6)
        risk_titles = _rss_titles("crypto hack lawsuit SEC exchange when:1d", limit=6)
        return (
            _etf_signal(etf_titles),
            _fed_signal(fed_titles),
            _risk_signal(risk_titles),
        )
    except Exception:
        logging.exception("Could not fetch news signals")
        return ()


def fetch_market_context() -> MarketContext:
    global _CONTEXT_CACHE
    now = time.time()
    if _CONTEXT_CACHE and now - _CONTEXT_CACHE[0] <= NEWS_CACHE_SECONDS:
        return _CONTEXT_CACHE[1]

    context = MarketContext(
        btc_dominance_pct=fetch_btc_dominance(),
        news_signals=fetch_news_signals(),
    )
    _CONTEXT_CACHE = (now, context)
    return context
