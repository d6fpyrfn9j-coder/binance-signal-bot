"""Optional on-chain exchange-flow data from CryptoQuant."""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any


CRYPTOQUANT_BASE_URL = "https://api.cryptoquant.com/v1"
CRYPTOQUANT_ASSETS = {
    "BTCUSDT": "btc",
    "ETHUSDT": "eth",
}


@dataclass(frozen=True)
class OnChainFlow:
    symbol: str
    asset: str
    netflow: float | None
    netflow_change_pct: float | None
    reserve: float | None
    reserve_change_pct: float | None


@dataclass(frozen=True)
class StablecoinReserve:
    reserve: float | None
    reserve_usd: float | None
    reserve_change_pct: float | None
    reserve_usd_change_pct: float | None


def _access_token() -> str:
    return (
        os.getenv("CRYPTOQUANT_API_KEY")
        or os.getenv("CRYPTOQUANT_ACCESS_TOKEN")
        or ""
    ).strip()


def cryptoquant_enabled() -> bool:
    return bool(_access_token())


def _read_cryptoquant(path: str, params: dict[str, str | int], timeout: int = 20) -> dict[str, Any]:
    token = _access_token()
    if not token:
        raise RuntimeError("CRYPTOQUANT_API_KEY is not configured")

    query = urllib.parse.urlencode(params)
    url = f"{CRYPTOQUANT_BASE_URL}{path}?{query}"
    request = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "User-Agent": "binance-signal-bot/1.0",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"CryptoQuant HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"CryptoQuant network error: {exc.reason}") from exc


def _sorted_data(response: dict[str, Any]) -> list[dict[str, Any]]:
    data = response.get("result", {}).get("data", [])
    if not isinstance(data, list):
        return []
    return sorted(
        (item for item in data if isinstance(item, dict)),
        key=lambda item: str(item.get("date", "")),
    )


def _number(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _latest_and_previous(data: list[dict[str, Any]], key: str) -> tuple[float | None, float | None]:
    values = [_number(item.get(key)) for item in data]
    values = [value for value in values if value is not None]
    if not values:
        return None, None
    if len(values) == 1:
        return values[-1], None
    return values[-1], values[-2]


def _change_pct(current: float | None, previous: float | None) -> float | None:
    if current is None or previous is None or previous == 0:
        return None
    return ((current - previous) / abs(previous)) * 100


def _metric_data(asset: str, metric: str, exchange: str, window: str, limit: int) -> list[dict[str, Any]]:
    response = _read_cryptoquant(
        f"/{asset}/exchange-flows/{metric}",
        {
            "exchange": exchange,
            "window": window,
            "limit": limit,
        },
    )
    return _sorted_data(response)


def fetch_onchain_flows(
    symbols: tuple[str, ...],
    exchange: str = "binance",
    window: str = "hour",
    limit: int = 3,
) -> dict[str, OnChainFlow]:
    if not cryptoquant_enabled():
        return {}

    flows: dict[str, OnChainFlow] = {}
    for symbol in symbols:
        asset = CRYPTOQUANT_ASSETS.get(symbol)
        if not asset:
            continue
        try:
            netflow_data = _metric_data(asset, "netflow", exchange, window, limit)
            reserve_data = _metric_data(asset, "reserve", exchange, window, limit)
            netflow, previous_netflow = _latest_and_previous(netflow_data, "netflow_total")
            reserve, previous_reserve = _latest_and_previous(reserve_data, "reserve")
            flows[symbol] = OnChainFlow(
                symbol=symbol,
                asset=asset.upper(),
                netflow=netflow,
                netflow_change_pct=_change_pct(netflow, previous_netflow),
                reserve=reserve,
                reserve_change_pct=_change_pct(reserve, previous_reserve),
            )
        except Exception:
            logging.exception("Could not fetch CryptoQuant on-chain flow for %s", symbol)
    return flows


def fetch_stablecoin_reserve(
    exchange: str = "binance",
    window: str = "hour",
    limit: int = 3,
) -> StablecoinReserve | None:
    if not cryptoquant_enabled():
        return None

    try:
        response = _read_cryptoquant(
            "/stablecoin/exchange-flows/reserve",
            {
                "exchange": exchange,
                "window": window,
                "limit": limit,
            },
        )
        data = _sorted_data(response)
        reserve, previous_reserve = _latest_and_previous(data, "reserve")
        reserve_usd, previous_reserve_usd = _latest_and_previous(data, "reserve_usd")
        return StablecoinReserve(
            reserve=reserve,
            reserve_usd=reserve_usd,
            reserve_change_pct=_change_pct(reserve, previous_reserve),
            reserve_usd_change_pct=_change_pct(reserve_usd, previous_reserve_usd),
        )
    except Exception:
        logging.exception("Could not fetch CryptoQuant stablecoin reserve")
        return None
