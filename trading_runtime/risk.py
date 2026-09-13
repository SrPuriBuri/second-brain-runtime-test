"""Whole-share sizing for limit brackets; no AI-supplied quantity is accepted."""

from dataclasses import dataclass
from decimal import Decimal, ROUND_FLOOR

from .config import SafetyError


def dec(value):
    try:
        result = Decimal(str(value))
        if not result.is_finite():
            raise ValueError
        return result
    except Exception:
        raise SafetyError("NON_FINITE_RISK_INPUT") from None


@dataclass(frozen=True)
class Size:
    quantity: int
    risk: Decimal
    notional: Decimal
    reward_risk: Decimal


def size_position(proposal, policy, buying_power, cash):
    entry, stop, target = map(
        dec, (proposal.entry_price_or_rule, proposal.stop_price, proposal.target_price)
    )
    if stop >= entry or stop <= 0:
        raise SafetyError("INVALID_STOP")
    if target <= entry:
        raise SafetyError("INVALID_TARGET")
    risk = entry - stop
    budget = dec(policy.virtual_risk_equity) * dec(policy.max_risk_fraction)
    capacity = min(dec(buying_power), dec(cash), dec(policy.max_position_notional))
    qty = int(
        min(
            budget / risk, capacity / entry, dec(policy.strategy_max_quantity)
        ).to_integral_value(rounding=ROUND_FLOOR)
    )
    if qty <= 0:
        raise SafetyError("INSUFFICIENT_BUYING_POWER")
    return Size(qty, risk * qty, entry * qty, (target - entry) / risk)
