"""Frozen Decimal price, sizing and protective-exit calculations."""

from decimal import Decimal, ROUND_FLOOR

from .numeric import monetary, exact_decimal, decimal_from_f64, floor_cent, ceil_cent


@monetary
def risk_prices(feature, parameters):
    distance = max(float(parameters["stop_atr_multiplier"]) * feature["atr"],
                   parameters["minimum_stop_fraction"] * feature["close"])
    if distance > parameters["maximum_stop_fraction"] * feature["close"]:
        return None
    d = decimal_from_f64(distance)
    close = exact_decimal(feature["close_text"])
    return floor_cent(close - d), floor_cent(close + Decimal(parameters["target_risk_multiple"]) * d)


@monetary
def entry_price(opened, costs):
    return ceil_cent(opened * (Decimal("1") + Decimal(costs["half_spread_bps"] + costs["slippage_bps"]) / Decimal("10000")))


@monetary
def exit_price(reference, costs):
    return floor_cent(reference * (Decimal("1") - Decimal(sum(costs.values())) / Decimal("10000")))


@monetary
def size_limits(entry, stop, volume, sizing):
    return {
        "risk": Decimal(sizing["risk_budget_per_trade"]) / (entry - stop),
        "notional": Decimal(sizing["max_notional"]) / entry,
        "cash": Decimal(sizing["virtual_cash"]) / entry,
        "quantity": Decimal(sizing["max_quantity"]),
        "volume": decimal_from_f64(sizing["bar_participation"]) * volume,
    }


@monetary
def enter(intent, bar, costs, sizing):
    opened = exact_decimal(bar.open_text)
    entry = entry_price(opened, costs)
    stop, target = intent["stop"], intent["target"]
    cap = floor_cent(exact_decimal(intent["feature"]["close_text"]) * Decimal("1.003"))
    if entry > cap:
        return None, "ENTRY_CAP"
    if not (opened > stop and entry > stop and target > entry):
        return None, "ENTRY_PRICE_RISK"
    if (target - entry) / (entry - stop) < Decimal("1.5"):
        return None, "ENTRY_RR"
    limits = size_limits(entry, stop, exact_decimal(intent["feature"]["last_volume_text"]), sizing)
    quantity = int(min(limits.values()).to_integral_value(rounding=ROUND_FLOOR))
    if quantity <= 0:
        return None, "ENTRY_ZERO_QUANTITY"
    return {"entry": entry, "quantity": quantity, "stop": stop, "target": target}, None


def protective(position, bar):
    opened, high, low, _, _ = bar.values
    if opened <= position["stop"]:
        return opened, "GAP_THROUGH_STOP"
    if low <= position["stop"]:
        return position["stop"], "STOP"
    if high > position["target"]:
        return position["target"], "TARGET"
    return None


@monetary
def net_r(position, net_exit):
    q = position["quantity"]
    return float(((net_exit - position["entry"]) * q) / ((position["entry"] - position["stop"]) * q))
