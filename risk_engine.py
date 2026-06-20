"""Futures position sizing and risk validation."""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum


class RiskStatus(str, Enum):
    SAFE = "SAFE"
    HIGH_RISK = "HIGH RISK"
    SKIP = "SKIP"
    NO_TRADE = "NO_TRADE"


@dataclass(frozen=True)
class RiskPlan:
    account_balance: float
    risk_pct: float
    risk_amount: float
    confidence: int | None
    side: str
    entry: float
    stop_loss: float
    tp1: float
    tp2: float
    rr: float
    stop_distance: float
    stop_distance_pct: float
    position_size: float
    notional_value: float
    suggested_margin: float
    suggested_leverage: int
    max_leverage: int
    daily_loss_pct: float
    daily_losing_trades: int
    max_daily_loss_pct: float
    max_daily_losing_trades: int
    status: RiskStatus
    reason: str


def risk_pct_for_confidence(confidence: int | None, max_risk_pct: float = 2.0) -> float:
    if confidence is None or confidence < 75:
        return 0.0
    if confidence < 85:
        return min(0.5, max_risk_pct)
    if confidence < 95:
        return min(1.0, max_risk_pct)
    return min(2.0, max_risk_pct)


def _empty_plan(
    side: str,
    entry: float,
    stop_loss: float,
    take_profit: float,
    account_balance: float,
    risk_pct: float,
    max_leverage: int,
    status: RiskStatus,
    reason: str,
    confidence: int | None = None,
    daily_loss_pct: float = 0.0,
    daily_losing_trades: int = 0,
    max_daily_loss_pct: float = 5.0,
    max_daily_losing_trades: int = 3,
) -> RiskPlan:
    risk_amount = account_balance * (risk_pct / 100) if account_balance > 0 else 0.0
    stop_distance = abs(entry - stop_loss) if entry > 0 and stop_loss > 0 else 0.0
    reward_distance = abs(take_profit - entry) if entry > 0 and take_profit > 0 else 0.0
    rr = reward_distance / stop_distance if stop_distance > 0 else 0.0
    stop_distance_pct = (stop_distance / entry) * 100 if entry > 0 else 0.0
    if side == "LONG" and stop_distance > 0:
        tp1 = entry + stop_distance
    elif side == "SHORT" and stop_distance > 0:
        tp1 = entry - stop_distance
    else:
        tp1 = take_profit

    return RiskPlan(
        account_balance=account_balance,
        risk_pct=risk_pct,
        risk_amount=risk_amount,
        confidence=confidence,
        side=side,
        entry=entry,
        stop_loss=stop_loss,
        tp1=tp1,
        tp2=take_profit,
        rr=rr,
        stop_distance=stop_distance,
        stop_distance_pct=stop_distance_pct,
        position_size=0.0,
        notional_value=0.0,
        suggested_margin=0.0,
        suggested_leverage=0,
        max_leverage=max(1, max_leverage),
        daily_loss_pct=daily_loss_pct,
        daily_losing_trades=daily_losing_trades,
        max_daily_loss_pct=max_daily_loss_pct,
        max_daily_losing_trades=max_daily_losing_trades,
        status=status,
        reason=reason,
    )


def _reject_plan(
    side: str,
    entry: float,
    stop_loss: float,
    take_profit: float,
    account_balance: float,
    risk_pct: float,
    max_leverage: int,
    status: RiskStatus,
    reason: str,
    confidence: int | None,
    daily_loss_pct: float,
    daily_losing_trades: int,
    max_daily_loss_pct: float,
    max_daily_losing_trades: int,
) -> RiskPlan:
    return _empty_plan(
        side,
        entry,
        stop_loss,
        take_profit,
        account_balance,
        risk_pct,
        max_leverage,
        status,
        reason,
        confidence,
        daily_loss_pct,
        daily_losing_trades,
        max_daily_loss_pct,
        max_daily_losing_trades,
    )


def calculate_futures_risk(
    side: str,
    entry: float,
    stop_loss: float,
    take_profit: float,
    account_balance: float = 500.0,
    risk_pct: float = 2.0,
    confidence: int | None = None,
    max_leverage: int = 5,
    min_rr: float = 1.8,
    max_stop_loss_pct: float = 5.0,
    high_margin_pct: float = 50.0,
    daily_loss_pct: float = 0.0,
    daily_losing_trades: int = 0,
    max_daily_loss_pct: float = 5.0,
    max_daily_losing_trades: int = 3,
) -> RiskPlan:
    side = side.upper()
    max_leverage = max(1, int(max_leverage))
    max_daily_losing_trades = max(1, int(max_daily_losing_trades))
    dynamic_risk_pct = risk_pct_for_confidence(confidence, risk_pct)

    if daily_loss_pct >= max_daily_loss_pct:
        return _reject_plan(
            side,
            entry,
            stop_loss,
            take_profit,
            account_balance,
            dynamic_risk_pct,
            max_leverage,
            RiskStatus.NO_TRADE,
            f"daily loss {daily_loss_pct:.1f}% >= {max_daily_loss_pct:.1f}%",
            confidence,
            daily_loss_pct,
            daily_losing_trades,
            max_daily_loss_pct,
            max_daily_losing_trades,
        )
    if daily_losing_trades >= max_daily_losing_trades:
        return _reject_plan(
            side,
            entry,
            stop_loss,
            take_profit,
            account_balance,
            dynamic_risk_pct,
            max_leverage,
            RiskStatus.NO_TRADE,
            f"daily losing trades {daily_losing_trades} >= {max_daily_losing_trades}",
            confidence,
            daily_loss_pct,
            daily_losing_trades,
            max_daily_loss_pct,
            max_daily_losing_trades,
        )

    if account_balance <= 0:
        return _reject_plan(
            side,
            entry,
            stop_loss,
            take_profit,
            account_balance,
            dynamic_risk_pct,
            max_leverage,
            RiskStatus.SKIP,
            "invalid balance",
            confidence,
            daily_loss_pct,
            daily_losing_trades,
            max_daily_loss_pct,
            max_daily_losing_trades,
        )
    if risk_pct <= 0:
        return _reject_plan(
            side,
            entry,
            stop_loss,
            take_profit,
            account_balance,
            dynamic_risk_pct,
            max_leverage,
            RiskStatus.SKIP,
            "invalid risk percent",
            confidence,
            daily_loss_pct,
            daily_losing_trades,
            max_daily_loss_pct,
            max_daily_losing_trades,
        )
    if dynamic_risk_pct <= 0:
        return _reject_plan(
            side,
            entry,
            stop_loss,
            take_profit,
            account_balance,
            dynamic_risk_pct,
            max_leverage,
            RiskStatus.SKIP,
            "confidence < 75",
            confidence,
            daily_loss_pct,
            daily_losing_trades,
            max_daily_loss_pct,
            max_daily_losing_trades,
        )
    if side not in {"LONG", "SHORT"}:
        return _reject_plan(
            side,
            entry,
            stop_loss,
            take_profit,
            account_balance,
            dynamic_risk_pct,
            max_leverage,
            RiskStatus.SKIP,
            "invalid side",
            confidence,
            daily_loss_pct,
            daily_losing_trades,
            max_daily_loss_pct,
            max_daily_losing_trades,
        )
    if entry <= 0 or stop_loss <= 0 or take_profit <= 0:
        return _reject_plan(
            side,
            entry,
            stop_loss,
            take_profit,
            account_balance,
            dynamic_risk_pct,
            max_leverage,
            RiskStatus.SKIP,
            "invalid price",
            confidence,
            daily_loss_pct,
            daily_losing_trades,
            max_daily_loss_pct,
            max_daily_losing_trades,
        )

    if side == "LONG" and (stop_loss >= entry or take_profit <= entry):
        return _reject_plan(
            side,
            entry,
            stop_loss,
            take_profit,
            account_balance,
            dynamic_risk_pct,
            max_leverage,
            RiskStatus.SKIP,
            "invalid LONG levels",
            confidence,
            daily_loss_pct,
            daily_losing_trades,
            max_daily_loss_pct,
            max_daily_losing_trades,
        )
    if side == "SHORT" and (stop_loss <= entry or take_profit >= entry):
        return _reject_plan(
            side,
            entry,
            stop_loss,
            take_profit,
            account_balance,
            dynamic_risk_pct,
            max_leverage,
            RiskStatus.SKIP,
            "invalid SHORT levels",
            confidence,
            daily_loss_pct,
            daily_losing_trades,
            max_daily_loss_pct,
            max_daily_losing_trades,
        )

    risk_amount = account_balance * (dynamic_risk_pct / 100)
    stop_distance = abs(entry - stop_loss)
    reward_distance = abs(take_profit - entry)
    stop_distance_pct = (stop_distance / entry) * 100
    rr = reward_distance / stop_distance if stop_distance else 0.0
    position_size = risk_amount / stop_distance if stop_distance else 0.0
    notional_value = position_size * entry
    required_leverage = notional_value / account_balance
    target_margin = account_balance * (high_margin_pct / 100)
    margin_leverage = math.ceil(notional_value / target_margin) if target_margin > 0 else max_leverage
    suggested_leverage = min(max_leverage, max(1, math.ceil(required_leverage), margin_leverage))
    suggested_margin = notional_value / suggested_leverage if suggested_leverage else 0.0
    tp1 = entry + stop_distance if side == "LONG" else entry - stop_distance

    skip_reasons: list[str] = []
    high_risk_reasons: list[str] = []
    if rr < min_rr:
        skip_reasons.append(f"R/R {rr:.1f} < {min_rr:.1f}")
    if stop_distance_pct > max_stop_loss_pct:
        skip_reasons.append(f"stop {stop_distance_pct:.1f}% > {max_stop_loss_pct:.1f}%")
    if required_leverage > max_leverage:
        skip_reasons.append(f"needs {required_leverage:.1f}x > max {max_leverage}x")

    margin_pct = (suggested_margin / account_balance) * 100 if account_balance else 0.0
    if stop_distance_pct >= max_stop_loss_pct * 0.75:
        high_risk_reasons.append(f"wide stop {stop_distance_pct:.1f}%")
    if margin_pct > high_margin_pct + 1e-9:
        high_risk_reasons.append(f"margin {margin_pct:.0f}% of balance")
    if suggested_leverage >= max_leverage and required_leverage >= max_leverage * 0.8:
        high_risk_reasons.append(f"near max leverage {suggested_leverage}x")

    if skip_reasons:
        status = RiskStatus.SKIP
        reason = "; ".join(skip_reasons)
    elif high_risk_reasons:
        status = RiskStatus.HIGH_RISK
        reason = "; ".join(high_risk_reasons)
    else:
        status = RiskStatus.SAFE
        reason = "risk ok"

    return RiskPlan(
        account_balance=account_balance,
        risk_pct=dynamic_risk_pct,
        risk_amount=risk_amount,
        confidence=confidence,
        side=side,
        entry=entry,
        stop_loss=stop_loss,
        tp1=tp1,
        tp2=take_profit,
        rr=rr,
        stop_distance=stop_distance,
        stop_distance_pct=stop_distance_pct,
        position_size=position_size,
        notional_value=notional_value,
        suggested_margin=suggested_margin,
        suggested_leverage=suggested_leverage,
        max_leverage=max_leverage,
        daily_loss_pct=daily_loss_pct,
        daily_losing_trades=daily_losing_trades,
        max_daily_loss_pct=max_daily_loss_pct,
        max_daily_losing_trades=max_daily_losing_trades,
        status=status,
        reason=reason,
    )
