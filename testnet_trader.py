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
LIVE_BASE_URL = "https://fapi.binance.com"
_QTY_STEP = {"BTCUSDT": 0.001, "ETHUSDT": 0.001}


_ACTIVE_MODE: str | None = None  # set per-cycle by trade_from_history when running a mode


def _mode() -> str:
    # When a specific mode is being run (dual-mode cycle), honor it; else read env
    # (first listed mode wins as the safe default — testnet before live).
    if _ACTIVE_MODE is not None:
        return _ACTIVE_MODE
    return os.getenv("TRADING_MODE", "").strip().lower().split(",")[0].strip()


def _enabled_modes() -> list[str]:
    """Modes to run this cycle. TRADING_MODE may be one mode or a comma list
    (e.g. 'testnet,live') so demo practice and the real account run side by side."""
    raw = os.getenv("TRADING_MODE", "").strip().lower()
    out: list[str] = []
    for part in raw.split(","):
        m = part.strip()
        if m in {"testnet", "live"} and m not in out:
            out.append(m)
    return out


def _is_live() -> bool:
    return _mode() == "live"


def _base_url() -> str:
    """Real exchange only when TRADING_MODE=live; otherwise the fake-money testnet."""
    return LIVE_BASE_URL if _is_live() else TESTNET_BASE_URL


def trading_enabled() -> bool:
    if _mode() not in {"testnet", "live"}:
        return False
    return bool(_api_key() and _api_secret())


def _api_key() -> str:
    name = "BINANCE_API_KEY" if _is_live() else "BINANCE_TESTNET_API_KEY"
    return (os.getenv(name) or "").strip()


def _api_secret() -> str:
    name = "BINANCE_API_SECRET" if _is_live() else "BINANCE_TESTNET_API_SECRET"
    return (os.getenv(name) or "").strip()


def _positions_path() -> Path:
    # Live keeps its own state file so real and demo positions never collide.
    if _is_live():
        return Path("live_positions.json")
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
    url = f"{_base_url()}{path}"
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


def _cancel_open_orders(symbol: str) -> None:
    """Cancel all resting orders for a symbol — clears a leftover reduceOnly stop/TP
    sibling so it can't fire against a future position."""
    try:
        _request("DELETE", "/fapi/v1/allOpenOrders", {"symbol": symbol}, signed=True)
    except Exception as exc:
        logging.info("Could not cancel open orders for %s: %s", symbol, str(exc)[-80:])


def _ensure_isolated(symbol: str) -> None:
    """Use ISOLATED margin so each position's risk is capped by its own margin
    (a bad trade can't drain the whole account)."""
    try:
        _request("POST", "/fapi/v1/marginType", {"symbol": symbol, "marginType": "ISOLATED"}, signed=True)
    except RuntimeError as exc:
        if "-4046" not in str(exc):  # -4046 = already isolated, fine
            logging.warning("Could not set ISOLATED for %s: %s", symbol, exc)


def _try_exchange_stops(symbol: str, direction: str, stop: float, target: float, qty: float) -> bool:
    """Place exchange-side stop-loss + take-profit so the position is protected even
    if the worker dies. Uses reduceOnly + quantity (the form real Binance accepts —
    closePosition=true is rejected with "use Algo Order API"). MARK_PRICE trigger
    avoids wick-based false fills. Falls back to software stops if still refused."""
    tick = {"BTCUSDT": 0.1, "ETHUSDT": 0.01}.get(symbol, 0.01)
    sp = round(round(stop / tick) * tick, 8)
    tp = round(round(target / tick) * tick, 8)
    qty = _round_step(abs(qty), _QTY_STEP.get(symbol, 0.001))
    close_side = "SELL" if direction == "LONG" else "BUY"
    try:
        _request("POST", "/fapi/v1/order", {"symbol": symbol, "side": close_side,
                 "type": "STOP_MARKET", "stopPrice": sp, "quantity": qty,
                 "reduceOnly": "true", "workingType": "MARK_PRICE"}, signed=True)
        _request("POST", "/fapi/v1/order", {"symbol": symbol, "side": close_side,
                 "type": "TAKE_PROFIT_MARKET", "stopPrice": tp, "quantity": qty,
                 "reduceOnly": "true", "workingType": "MARK_PRICE"}, signed=True)
        return True
    except Exception as exc:
        logging.warning("Exchange stops unavailable for %s (software fallback): %s", symbol, str(exc))
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
    _cancel_open_orders(symbol)  # clean slate — drop any stale stop/TP from a prior trade
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
    exchange_stops = _try_exchange_stops(symbol, direction, stop, target, qty)
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
        # Live: the exchange stop/TP runs the exit; software must NOT also manage it
        # (avoids double-close). Just detect when the exchange closed it, then clean up.
        if pos.get("exchange_stops"):
            if abs(get_position_amount(symbol)) <= 0:
                _cancel_open_orders(symbol)  # clear the leftover stop/TP sibling
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


def live_permission_check() -> str:
    """Boot diagnostic: confirm the LIVE key can READ (balance) AND TRADE (set
    leverage) from this server's IP. Opens NO position — set leverage is just a
    setting. Used to verify the Binance key's Futures + IP allowlist is correct."""
    global _ACTIVE_MODE
    if "live" not in _enabled_modes():
        return "live mode off — skip"
    prev = _ACTIVE_MODE
    _ACTIVE_MODE = "live"
    try:
        try:
            bal = get_balance_usdt()
        except Exception as exc:
            return f"READ FAILED (key/IP): {str(exc)[-110:]}"
        try:
            lev = int(os.getenv("TESTNET_LEVERAGE", "5"))
            _request("POST", "/fapi/v1/leverage", {"symbol": "BTCUSDT", "leverage": lev}, signed=True)
        except Exception as exc:
            return f"READ ok (balance={bal}) but TRADE BLOCKED: {str(exc)[-110:]}"
        return f"OK — READ ok (balance={bal}) + TRADE ok — Futures+IP allowlist WORKS"
    finally:
        _ACTIVE_MODE = prev


def trade_from_history() -> list[str]:
    """Run trading for each enabled mode this cycle. With TRADING_MODE='testnet,live'
    the demo (fake money) and the real account run independently, side by side —
    separate keys, balances, positions; they only share the read-only signal list."""
    global _ACTIVE_MODE
    modes = _enabled_modes()
    if not modes:
        return []
    events: list[str] = []
    for m in modes:
        _ACTIVE_MODE = m
        try:
            label = "REAL" if m == "live" else "DEMO"
            for e in _trade_one_mode():
                events.append(f"[{label}] {e}")
        except Exception:
            logging.exception("Trading step failed for mode %s", m)
        finally:
            _ACTIVE_MODE = None
    return events


def _trade_one_mode() -> list[str]:
    """Bridge for ONE mode: manage open trades, then open any fresh 'AÇ' signal the
    bot just wrote to its futures signal history. Uses the active mode's keys/state."""
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
    # Live circuit breaker: if the account dropped below the floor, stop opening new
    # trades (still manage existing). Set LIVE_MIN_BALANCE>0 to enable.
    min_balance = float(os.getenv("LIVE_MIN_BALANCE", "0"))
    if min_balance > 0 and balance < min_balance:
        events.append(f"⛔ CIRCUIT BREAKER: balans {balance:.2f} < {min_balance:.0f} — yeni trade yox")
        return events
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
