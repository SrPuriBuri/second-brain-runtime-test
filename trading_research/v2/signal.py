"""Rank exactly one candidate; no fallback after later eligibility failure."""

from datetime import timedelta

from .fills import risk_prices
from .models import provenance_fields


def build_intent(panel, session, spec, delay, *, control=False):
    p = spec.base["simulation"]["signal"]["parameters"]
    ranked = sorted(panel["symbols"], key=lambda s: (panel["symbols"][s]["z"], s))
    symbol = "SPY" if control else ranked[0]
    f = panel["symbols"][symbol]
    if not control:
        if f["z"] > p["z_threshold"]:
            return None, "NO_SIGNAL_Z"
        if f["relative_turn"] <= 0:
            return None, "NO_SIGNAL_RELATIVE_TURN"
        if panel["median"] <= 0 or sum(panel["symbols"][s]["move"] > 0 for s in ("SPY", "QQQ", "IWM")) < p["direction_confirmation_required"]:
            return None, "NO_SIGNAL_COMMON_STATE"
        if not 5 <= f["close"] <= 1000 or f["dollar_volume"] < 10000000:
            return None, "NO_SIGNAL_PRICE_VOLUME"
    due = panel["at"] + timedelta(minutes=delay)
    if due + timedelta(minutes=p["holding_tradable_minutes"]) >= session.close - timedelta(minutes=45):
        return None, "NO_SIGNAL_TIME"
    prices = risk_prices(f, p)
    if prices is None:
        return None, "NO_SIGNAL_RISK"
    stop, target = prices
    return {**provenance_fields(session), "symbol": symbol, "signal_at": panel["at"], "due": due,
            "stop": stop, "target": target, "feature": f}, None
