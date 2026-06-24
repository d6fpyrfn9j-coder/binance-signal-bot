"""Binance Futures TESTNET auto-trader (fake money only).

SAFETY: HARD-LOCKED to the Binance Futures testnet base URL and refuses to trade
unless TRADING_MODE=testnet with keys present, so it can never touch real money.
Lets the signal bot practice opening/closing futures positions with fake money.

This testnet rejects exchange-side STOP_MARKET/TAKE_PROFIT_MARKET orders, so stops,
take-profit, break-even and trailing are managed in SOFTWARE: every cycle the bot
checks open positions and closes them with a market order when a level is hit.

Needs TESTNET keys (https://testnet.binancefuture.com). Env:
BINANCE_TESTNET_API_KEY, BINANCE_TESTNET_API_SECRET, TRADING_MODE=testnet.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import json
import logging
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


TESTNET_BASE_URL = "https://testnet.binancefuture.com"
_QTY_STEP = {"BTCUSDT": 0.001, "ETHUSDT": 0.001}


def trading_enabled() -> bool:
    if os.getenv("TRADING_MODE", "").strip().lower() != "testnet":
        return False
    return bool(_api_key() and _api_secret())


def _api_key() -> str:
    return (os.getenv("BINANCE_TESTNET_API_KEY") or "").strip()


def _api_secret() -> str:
    return (os.getenv("BINANCE_TESTNET_API_SECRET") or "").strip()


def _positions_path() -> Path:
    return Path(os.getenv("TESTNET_POSITIONS_FILE", "testnet_positions.json"))


def _round_step(value: float, step: float) -> float:
    return round(round(value / step) * step, 8) if step > 0 else value


def _request(method: str, path: str, params: dict | None = None, signed: bool = False, timeout: int = 15):
    params = dict(params or {})
    headers = {"X-MBX-APIKEY": _api_key()} if (signed or _api_key()) else {}
    if signed:
        params["timestamp"] = int(time.time() * 1000)
        params["recvWindow"] = 5000
        query = urllib.parse.urlencode(params)
        signature = hmac.new(_api_secret().encode(), query.encode(), hashlib.sha256).hexdigest()
        query = f"{query}&signature={signature}"
    else:
        query = urllib.parse.urlencode(params)
    url = f"{TESTNET_BASE_URL}{path}"
    data = None
    if method == "GET":
        url = f"{url}?{query}" if query else url
    else:
        data = query.encode()
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"Testnet HTTP {exc.code}: {exc.read().decode('utf-8', 'replace')}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Testnet network error: {exc.reason}") from exc


# --- account / market helpers ---

def get_balance_usdt() -> float | None:
    for row in _request("GET", "/fapi/v2/balance", signed=True) or []:
        if row.get("asset") == "USDT":
            return float(row.get("balance", 0) or 0)
    return None


def get_position_amount(symbol: str) -> float:
    for row in _request("GET", "/fapi/v2/positionRisk", {"symbol": symbol}, signed=True) or []:
        if row.get("symbol") == symbol:
            return float(row.get("positionAmt", 0) or 0)
    return 0.0


def get_price(symbol: str) -> float:
    raw = _request("GET", "/fapi/v1/ticker/price", {"symbol": symbol})
    return float(raw["price"])


def set_leverage(symbol: str, leverage: int) -> None:
    try:
        _request("POST", "/fapi/v1/leverage", {"symbol": symbol, "leverage": int(leverage)}, signed=True)
    except Exception:
        logging.exception("Could not set leverage for %s", symbol)


def _market(symbol: str, side: str, quantity: float, reduce_only: bool = False) -> dict:
    params = {"symbol": symbol, "side": side, "type": "MARKET", "quantity": quantity}
    if reduce_only:
        params["reduceOnly"] = "true"
    return _request("POST", "/fapi/v1/order", params, signed=True)


def _ensure_isolated(symbol: str) -> None:
    """Use ISOLATED margin so each position's risk is capped by its own margin
    (a bad trade can't drain the whole account)."""
    try:
        _request("POST", "/fapi/v1/marginType", {"symbol": symbol, "marginType": "ISOLATED"}, signed=True)
    except RuntimeError as exc:
        if "-4046" not in str(exc):  # -4046 = already isolated, fine
            logging.warning("Could not set ISOLATED for %s: %s", symbol, exc)


def _try_exchange_stops(symbol: str, direction: str, stop: float, target: float) -> bool:
    """Place exchange-side stop-loss + take-profit so the position is protected even
    if the worker dies. Works on real Binance; this testnet rejects it (-4120), so we
    return False and the software manager handles stops instead."""
    tick = {"BTCUSDT": 0.1, "ETHUSDT": 0.01}.get(symbol, 0.01)
    sp = round(round(stop / tick) * tick, 8)
    tp = round(round(target / tick) * tick, 8)
    close_side = "SELL" if direction == "LONG" else "BUY"
    try:
        _request("POST", "/fapi/v1/order", {"symbol": symbol, "side": close_side,
                 "type": "STOP_MARKET", "stopPrice": sp, "closePosition": "true"}, signed=True)
        _request("POST", "/fapi/v1/order", {"symbol": symbol, "side": close_side,
                 "type": "TAKE_PROFIT_MARKET", "stopPrice": tp, "closePosition": "true"}, signed=True)
        return True
    except Exception as exc:
        logging.info("Exchange stops unavailable for %s (software fallback): %s", symbol, str(exc)[-80:])
        return False


# --- software-managed positions ---

def _load() -> dict:
    p = _positions_path()
    if not p.exists():
        return {}
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _save(d: dict) -> None:
    try:
        _positions_path().write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        logging.exception("Could not save testnet positions")


def execute_signal(symbol: str, direction: str, entry: float, stop: float, target: float,
                   quantity: float, leverage: int) -> str:
    """Open a testnet market position for a LONG/SHORT signal if none is open."""
    if not trading_enabled():
        return "trading disabled (need TRADING_MODE=testnet + keys)"
    direction = direction.upper()
    if direction not in {"LONG", "SHORT"}:
        return "not a directional signal"
    state = _load()
    if symbol in state or abs(get_position_amount(symbol)) > 0:
        return f"{symbol}: position already open"
    qty = _round_step(quantity, _QTY_STEP.get(symbol, 0.001))
    if qty <= 0:
        return f"{symbol}: quantity rounds to 0"
    _ensure_isolated(symbol)
    set_leverage(symbol, leverage)
    side = "BUY" if direction == "LONG" else "SELL"
    try:
        _market(symbol, side, qty)
    except Exception as exc:
        logging.exception("Testnet open failed for %s", symbol)
        return f"{symbol}: open failed: {exc}"
    fill = get_price(symbol)
    # Try to place real stop-loss + take-profit on the exchange (real protection).
    # On this testnet it's rejected, so the software manager handles stops instead.
    exchange_stops = _try_exchange_stops(symbol, direction, stop, target)
    state[symbol] = {
        "direction": direction, "qty": qty, "entry": fill,
        "stop": stop, "target": target, "init_stop": stop,
        "extreme": fill, "opened_at": int(time.time()),
        "exchange_stops": exchange_stops,
    }
    _save(state)
    stop_kind = "birja-stop" if exchange_stops else "proqram-stop"
    return f"AÇILDI {direction} {qty} {symbol} @ {fill:.2f} | ISOLATED | stop {stop:.2f} ({stop_kind}) | hedef {target:.2f}"


def manage_open_positions() -> list[str]:
    """Each cycle: apply break-even + trailing, and close positions that hit stop/target."""
    if not trading_enabled():
        return []
    state = _load()
    events: list[str] = []
    for symbol, pos in list(state.items()):
        try:
            price = get_price(symbol)
        except Exception:
            continue
        # On real Binance an exchange stop/TP may have already closed it — clean up.
        if pos.get("exchange_stops") and abs(get_position_amount(symbol)) <= 0:
            events.append(f"{symbol} bağlandı (birja stop/hedef)")
            del state[symbol]; _save(state)
            continue
        d = pos["direction"]; entry = pos["entry"]; stop = pos["stop"]; target = pos["target"]
        risk = abs(entry - pos["init_stop"]) or (entry * 0.005)
        long = d == "LONG"
        # track best price reached
        pos["extreme"] = max(pos["extreme"], price) if long else min(pos["extreme"], price)
        profit = (price - entry) if long else (entry - price)
        # break-even: at +1R move stop to entry
        if profit >= risk:
            stop = max(stop, entry) if long else min(stop, entry)
        # trailing: once +1.5R, trail stop 1R behind the best price
        if profit >= 1.5 * risk:
            trail = pos["extreme"] - risk if long else pos["extreme"] + risk
            stop = max(stop, trail) if long else min(stop, trail)
        pos["stop"] = stop
        hit_stop = price <= stop if long else price >= stop
        hit_target = price >= target if long else price <= target
        if hit_stop or hit_target:
            try:
                _market(symbol, "SELL" if long else "BUY", pos["qty"], reduce_only=True)
            except Exception as exc:
                events.append(f"{symbol}: bağlama xətası {exc}")
                continue
            pnl = profit * pos["qty"] if (hit_target or stop >= entry if long else stop <= entry) else -risk * pos["qty"]
            why = "HEDEF ✅" if hit_target else ("BREAK-EVEN/TRAILING 🟡" if (stop >= entry if long else stop <= entry) else "STOP 🔴")
            events.append(f"{symbol} BAĞLANDI {why} @ {price:.2f} | P&L ~{pnl:+.2f} USDT")
            del state[symbol]
        _save(state)
    return events


def _risk_quantity(balance: float, entry: float, stop: float, risk_pct: float,
                   leverage: int, symbol: str) -> float:
    dist = abs(entry - stop)
    if dist <= 0 or balance <= 0 or entry <= 0:
        return 0.0
    qty = (balance * risk_pct / 100.0) / dist
    qty = min(qty, (balance * leverage) / entry)  # cap notional to balance*leverage
    return _round_step(qty, _QTY_STEP.get(symbol, 0.001))


def trade_from_history() -> list[str]:
    """Bridge: manage open testnet trades, then open any fresh 'AÇ' signal the bot
    just wrote to its futures signal history. Driven each worker cycle."""
    if not trading_enabled():
        return []
    risk_pct = float(os.getenv("TESTNET_RISK_PCT", "1.5"))
    leverage = int(os.getenv("TESTNET_LEVERAGE", "5"))
    max_age = int(os.getenv("TESTNET_SIGNAL_MAX_AGE", "900"))

    events = manage_open_positions()

    hp = Path(os.getenv("FUTURES_SIGNAL_HISTORY_FILE", "futures_signal_history.json"))
    if not hp.exists():
        return events
    try:
        records = json.loads(hp.read_text(encoding="utf-8"))
    except Exception:
        return events
    if not isinstance(records, list):
        return events

    balance = get_balance_usdt() or 0.0
    now = time.time()
    for rec in records[-25:]:
        if not isinstance(rec, dict) or rec.get("action") != "TRADE" or rec.get("status") != "open":
            continue
        side = rec.get("side")
        symbol = rec.get("symbol")
        entry, stop, target = rec.get("entry"), rec.get("stop"), rec.get("target")
        if side not in {"LONG", "SHORT"} or not (symbol and entry and stop and target):
            continue
        if symbol in _load():
            continue
        try:
            created = dt.datetime.fromisoformat(str(rec.get("created_at", "")).replace("Z", "+00:00")).timestamp()
        except ValueError:
            continue
        if now - created > max_age:
            continue
        qty = _risk_quantity(balance, float(entry), float(stop), risk_pct, leverage, symbol)
        if qty <= 0:
            continue
        events.append(execute_signal(symbol, side, float(entry), float(stop), float(target), qty, leverage))
    return events
