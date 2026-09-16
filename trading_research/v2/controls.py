"""Independent synthetic paths; no baseline-fill inheritance or alternative dates."""

from .metrics import summarize_ledger
from .models import NY, require_synthetic
from .simulator import simulate_session, no_trade
from trading_runtime.config import SafetyError


def evaluate_synthetic_sessions(sessions, spec, *, variant="CSLC_L30", scenario="BASELINE",
                                delay_diagnostic=False, control=False):
    """D1-only in-memory synthetic fixture evaluation, never an archive adapter."""
    sessions = tuple(sessions)
    for s in sessions:
        require_synthetic(s)
    dates = [str(s.open.astimezone(NY).date()) for s in sessions]
    if len(set(dates)) != len(dates):
        raise SafetyError("D1_SECOND_ENTRY_DAY")
    pairs = sorted(zip(dates, sessions))
    kind = "TIME_MATCHED_SPY" if control else "DELAY_DIAGNOSTIC" if delay_diagnostic else "STRATEGY"
    records = [{**simulate_session(s, spec, variant, scenario, delay_diagnostic=delay_diagnostic,
                                  control=control), "stage": "SYNTHETIC_CONFORMANCE"} for _, s in pairs]
    summary = summarize_ledger(records, sorted(dates), spec, stage="SYNTHETIC_CONFORMANCE",
                               variant=variant, scenario=scenario, record_type=kind)
    return {"records": records, "summary": summary}


def no_trade_control(sessions):
    return no_trade(sessions)
