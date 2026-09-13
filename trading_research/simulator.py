"""Deterministic long-only cash simulation; no broker interface exists here."""

from datetime import timedelta
import math

from .candidates import signal
from .features import build_features
from .universe import eligible


def exit_assumption(position, bar):
    """Pessimistic ordering; a target touch alone does not prove a limit fill."""
    if bar.o <= position["stop"]:
        return bar.o, "GAP_THROUGH_STOP"
    if bar.low <= position["stop"]:
        return position["stop"], "STOP"  # Also wins when target is crossed.
    if bar.h > position["target"]:
        return position["target"], "TARGET"
    return None


def simulate(dataset, days, variant, protocol, policy, costs, delay=None):
    trades, rejected = [], {}
    cash = float(protocol["initial_cash"])
    delay = protocol["delay_minutes"] if delay is None else delay
    if delay < 5 or delay % 5:
        raise ValueError("ENTRY_DELAY_MUST_BE_POSITIVE_BAR_MULTIPLE")
    feature_cache = getattr(dataset, "_feature_cache", {})
    dataset._feature_cache = feature_cache

    def reject(code):
        rejected[code] = rejected.get(code, 0) + 1

    for day in sorted(days):
        session = dataset.sessions[day]
        flat = session.close - timedelta(minutes=protocol["force_flat_minutes"])
        active, pending = {}, {}
        daily_entries, daily_loss = 0, 0.0
        halted = False

        def finish(symbol, at, reference, reason):
            nonlocal cash, daily_loss
            p = active.pop(symbol)
            exit_price = costs.exit(reference) if reference is not None else None
            pnl = (
                (exit_price - p["entry"]) * p["quantity"]
                if exit_price is not None
                else None
            )
            if pnl is not None:
                cash += exit_price * p["quantity"]
                daily_loss += max(0.0, -pnl)
            # An unknown exit does not release fictional buying power.
            trades.append(
                {
                    "day": day,
                    "symbol": symbol,
                    "side": "BUY",
                    "signal_at": p["signal_at"].isoformat(),
                    "entry_at": p["entry_at"].isoformat(),
                    "exit_at": at.isoformat(),
                    "entry": p["entry"],
                    "exit": exit_price,
                    "stop": p["stop"],
                    "target": p["target"],
                    "quantity": p["quantity"],
                    "initial_planned_risk": p["planned_risk"] * p["quantity"],
                    "r": pnl / (p["planned_risk"] * p["quantity"])
                    if pnl is not None
                    else None,
                    "reason": reason,
                    "regime": p["regime"],
                    "entry_bucket": str(p["offset"]),
                    "minutes": (at - p["entry_at"]).total_seconds() / 60,
                }
            )

        at = session.open
        while at <= flat:
            # Only a completed bar can resolve its intrabar ordering at this instant.
            previous = at - timedelta(minutes=5)
            for symbol in list(active):
                p = active[symbol]
                if previous < p["entry_at"]:
                    continue
                bar = dataset.bars.get(symbol, {}).get(day, {}).get(previous)
                if bar is None:
                    finish(symbol, at, None, "INDETERMINATE_MISSING_BAR")
                    halted = True
                    continue
                result = exit_assumption(p, bar)
                if result:
                    finish(symbol, at, *result)
            if at == flat:
                for symbol in list(active):
                    bar = dataset.bars.get(symbol, {}).get(day, {}).get(at)
                    finish(
                        symbol,
                        at,
                        bar.o if bar else None,
                        "FORCE_FLAT" if bar else "INDETERMINATE_FORCE_FLAT",
                    )
                pending.clear()
                break
            for symbol in sorted(list(pending)):
                p = pending[symbol]
                if p["due"] != at:
                    continue
                del pending[symbol]
                if (
                    halted
                    or symbol in active
                    or len(active) >= policy.max_positions
                    or daily_entries >= policy.max_new_positions
                ):
                    reject("PORTFOLIO_CAP")
                    continue
                bar = dataset.bars.get(symbol, {}).get(day, {}).get(at)
                if bar is None:
                    reject("MISSING_ENTRY_BAR")
                    continue
                entry = costs.entry(bar.o)
                if (
                    entry > p["reference"] * (1 + protocol["max_entry_premium"])
                    or bar.o <= p["stop"]
                    or (p["target"] - entry) / (entry - p["stop"])
                    < policy.min_reward_risk
                ):
                    reject("LIMIT_OR_RISK_REJECTED")
                    continue
                quantity = math.floor(
                    min(
                        policy.virtual_risk_equity
                        * policy.max_risk_fraction
                        / (entry - p["stop"]),
                        policy.max_position_notional / entry,
                        cash / entry,
                        policy.strategy_max_quantity,
                        p["last_volume"] * protocol["bar_participation_cap"],
                    )
                )
                open_risk = sum(
                    (x["entry"] - x["stop"]) * x["quantity"] for x in active.values()
                )
                if (
                    quantity <= 0
                    or daily_loss + open_risk + quantity * (entry - p["stop"])
                    > policy.virtual_risk_equity * policy.daily_risk_fraction
                ):
                    reject("CASH_OR_DAILY_RISK_CAP")
                    continue
                active[symbol] = {
                    **p,
                    "entry": entry,
                    "entry_at": at,
                    "quantity": quantity,
                }
                cash -= entry * quantity
                daily_entries += 1
            offset = int((at - session.open).total_seconds() / 60)
            if (
                not halted
                and offset in protocol["decision_offsets"]
                and at + timedelta(minutes=delay) < flat
            ):
                for symbol in sorted(protocol["symbols"]):
                    if symbol in active or symbol in pending:
                        continue
                    key = symbol, day, offset, protocol["history_sessions"]
                    if key not in feature_cache:
                        feature_cache[key] = build_features(
                            dataset, symbol, day, offset, protocol["history_sessions"]
                        )
                    f = feature_cache[key]
                    if not eligible(f, protocol, policy):
                        reject("INVALID_OR_MISSING_PAST_DATA")
                        continue
                    setup = signal(f, variant, protocol, policy.min_reward_risk)
                    if setup:
                        pending[symbol] = {
                            **setup,
                            "signal_at": at,
                            "due": at + timedelta(minutes=delay),
                            "regime": f.regime,
                            "offset": offset,
                            "last_volume": f.last_volume,
                        }
            at += timedelta(minutes=5)
        assert not active, "NO_OVERNIGHT_SIMULATION_STATE"
    return {
        "trades": trades,
        "rejections": rejected,
        "sessions": len(days),
        "session_minutes": sum(
            (dataset.sessions[d].close - dataset.sessions[d].open).total_seconds() / 60
            for d in days
        ),
        "variant_id": variant.id,
        "delay_minutes": delay,
        "remaining_positions": 0,
    }
