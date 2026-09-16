"""One provenance-bound session, one intent, one position, no broker interface."""

from datetime import timedelta

from trading_runtime.config import SafetyError
from .bindings import PINS
from .clock import STEP, exit_boundary, fully_halted, halted, timing
from .features import panel
from .fills import enter, protective, exit_price, net_r
from .models import NY, require_research_session, provenance_fields
from .signal import build_intent


def simulate_intent(session, spec, intent, scenario):
    require_research_session(session)
    costs = spec.base["costs"][scenario]
    at = intent["due"]
    record = {**PINS, **provenance_fields(session), "symbol": intent["symbol"],
              "session": str(session.open.astimezone(NY).date()), "scenario": scenario,
              "signal_timestamp": intent["signal_at"].isoformat(), "R": None,
              "integrity_violations": 0}

    def rejected(reason, *, indeterminate=False):
        status = "INDETERMINATE_FORCE_FLAT" if reason == "INDETERMINATE_FORCE_FLAT" else "INDETERMINATE"
        return {**record, "status": status if indeterminate else "NO_FILL",
                "reason": reason, "integrity_violations": int(indeterminate)}

    if halted(at, session.halts):
        return rejected("ENTRY_DUE_HALTED")
    bar = session.bar(intent["symbol"], at)
    if bar is None:
        return rejected("INDETERMINATE_ENTRY", indeterminate=True)
    position, reason = enter(intent, bar, costs, spec.base["simulation"]["sizing"])
    if position is None:
        return rejected(reason)
    entry_at = at
    flat = session.close - timedelta(minutes=45)
    until = exit_boundary(entry_at, session.close, session.halts,
                          spec.base["simulation"]["signal"]["parameters"]["holding_tradable_minutes"])
    ratio = intent["feature"]["atr"] / intent["feature"]["close"]
    record.update(entry_timestamp=entry_at.isoformat(), entry=str(position["entry"]),
                  stop=str(position["stop"]), target=str(position["target"]),
                  quantity=position["quantity"], side="LONG",
                  volatility_regime="LOW" if ratio <= .0005 else "NORMAL" if ratio <= .001 else "HIGH")

    def finish(reference, reason, start, end, exact):
        net = exit_price(reference, costs)
        return {**record, **timing(entry_at, start, end, session.halts, exact=exact),
                "status": "SCORED", "reason": reason, "gross_exit": str(reference),
                "net_exit": str(net), "R": net_r(position, net)}

    while at < until:
        if fully_halted(at, at + STEP, session.halts):
            at += STEP
            continue
        bar = session.bar(intent["symbol"], at)
        if bar is None:
            return rejected("INDETERMINATE_POST_ENTRY", indeterminate=True)
        outcome = protective(position, bar)
        if outcome:
            # C3: OHLC-only protective evidence never claims an exact tick time.
            return finish(*outcome, bar.start, bar.end, False)
        at += STEP
    if halted(until, session.halts):
        return rejected("INDETERMINATE_FORCE_FLAT" if until == flat else "INDETERMINATE_TIME_EXIT", indeterminate=True)
    bar = session.bar(intent["symbol"], until)
    if bar is None:
        return rejected("INDETERMINATE_FORCE_FLAT" if until == flat else "INDETERMINATE_TIME_EXIT", indeterminate=True)
    return finish(bar.values[0], "FORCE_FLAT" if until == flat else "TIME_EXIT", until, until, True)


def simulate_session(session, spec, variant="CSLC_L30", scenario="BASELINE", *, delay_diagnostic=False, control=False):
    require_research_session(session)
    if scenario not in spec.base["costs"]:
        raise SafetyError("D1_COST_SCENARIO")
    if (control or delay_diagnostic) and variant != spec.base["selection"]["canonical_id"]:
        raise SafetyError("D1_CANONICAL_ONLY")
    if delay_diagnostic and (scenario != "STRESS" or control):
        raise SafetyError("D1_DIAGNOSTIC_CONFIG")
    delay = spec.base["simulation"]["delay_stress_minutes" if delay_diagnostic else "earliest_entry_delay_minutes"]
    features, reason = panel(session, spec, variant)
    intent = None
    if features:
        intent, reason = build_intent(features, session, spec, delay)
    if intent is not None and control:
        intent, reason = build_intent(features, session, spec, delay, control=True)
    if intent is None:
        return {**PINS, **provenance_fields(session), "session": str(session.open.date()),
                "status": "NO_SIGNAL", "reason": reason, "R": None,
                "variant": variant, "scenario": scenario, "integrity_violations": 0,
                "record_type": "TIME_MATCHED_SPY" if control else "DELAY_DIAGNOSTIC" if delay_diagnostic else "STRATEGY"}
    return {**simulate_intent(session, spec, intent, scenario), "variant": variant,
            "record_type": "TIME_MATCHED_SPY" if control else "DELAY_DIAGNOSTIC" if delay_diagnostic else "STRATEGY"}


def no_trade(sessions):
    sessions = tuple(sessions)
    for session in sessions:
        require_research_session(session)
    return [{**PINS, **provenance_fields(s), "session": str(s.open.date()),
             "status": "NO_TRADE", "daily_R": 0.0, "R": None} for s in sessions]
