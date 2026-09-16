import pytest

from trading_research.v2.clock import timing, exit_boundary, overlap_seconds
from trading_research.v2.simulator import simulate_intent, simulate_session, no_trade
from trading_research.v2.models import SyntheticSession
from trading_runtime.config import SafetyError
from .conftest import at, bar, session, halt, intent, feature_panel


def test_golden_c3_normal():
    t = timing(at("13:05"), at("13:10"), at("13:15"), (), exact=False)
    assert t["execution_time_precision"] == "BAR_INTERVAL"
    assert t["execution_timestamp"] is None
    assert t["execution_time_lower_bound"] == at("13:10").isoformat()
    assert t["execution_time_upper_bound"] == at("13:15").isoformat()
    assert t["confirmation_timestamp"] == t["accounting_exit_timestamp"] == at("13:15").isoformat()
    assert t["time_in_market_wall_lower_bound"] == 5
    assert t["time_in_market_wall_upper_bound"] == t["time_in_market_wall_minutes"] == 10
    assert t["time_in_market_tradable_minutes"] == 10


def test_golden_c3_halt_and_entry_bar():
    t = timing(at("13:05"), at("13:10"), at("13:15"), [(at("13:11"), at("13:14"))], exact=False)
    assert t["time_in_market_wall_minutes"] == 10
    assert t["time_in_market_tradable_minutes"] == 7
    t = timing(at("13:05"), at("13:05"), at("13:10"), (), exact=False)
    assert t["time_in_market_wall_lower_bound"] == 0
    assert t["time_in_market_wall_minutes"] == 5


def test_golden_exact_exit():
    t = timing(at("13:05"), at("13:35"), at("13:35"), (), exact=True)
    assert t["execution_time_precision"] == "EXACT"
    assert t["execution_time_lower_bound"] == t["execution_time_upper_bound"] == t["execution_timestamp"]
    assert t["time_in_market_wall_minutes"] == 30


def test_golden_clock_pause_wall_cap_and_union():
    events = [(at("13:11"), at("13:14"))]
    assert exit_boundary(at("13:05"), at("16:00"), events, 30) == at("13:40")
    assert exit_boundary(at("12:00"), at("13:00"), [(at("12:05"), at("12:14"))], 30) == at("12:15")
    assert overlap_seconds(at("13:05"), at("13:15"), events + [(at("13:12"), at("13:15"))]) == 240


@pytest.mark.parametrize("scenario,expected_entry", [("BASELINE", "100.05"), ("STRESS", "100.10"), ("SEVERE", "100.20")])
def test_golden_time_exit_costs(spec, scenario, expected_entry):
    r = simulate_intent(session(), spec, intent(), scenario)
    assert r["entry"] == expected_entry
    assert r["reason"] == "TIME_EXIT"
    assert r["execution_time_precision"] == "EXACT"
    assert r["accounting_exit_timestamp"] == at("13:35").isoformat()
    assert r["time_in_market_tradable_minutes"] == 30
    assert r["side"] == "LONG"


@pytest.mark.parametrize("time,o,h,lo,reason,account", [
    ("13:05", "100", "103", "98", "STOP", "13:10"),
    ("13:10", "98", "103", "97", "GAP_THROUGH_STOP", "13:15"),
    ("13:10", "100", "103", "99", "TARGET", "13:15"),
])
def test_golden_intrabar_exits(spec, time, o, h, lo, reason, account):
    r = simulate_intent(session(replacements={("DIA", at(time)): bar(time, o=o, h=h, lo=lo)}), spec, intent(), "BASELINE")
    assert r["reason"] == reason
    assert r["execution_time_precision"] == "BAR_INTERVAL"
    assert r["accounting_exit_timestamp"] == at(account).isoformat()
    assert r["execution_timestamp"] is None


def test_invariant_time_boundary_does_not_read_future_high_low(spec):
    dangerous = bar("13:35", h="999", lo="1")
    r = simulate_intent(session(replacements={("DIA", at("13:35")): dangerous}), spec, intent(), "BASELINE")
    assert r["reason"] == "TIME_EXIT"


def test_invariant_exact_due_no_wait(spec):
    r = simulate_intent(session(omit={("DIA", at("13:05"))}), spec, intent(), "BASELINE")
    assert r["status"] == "INDETERMINATE"
    assert r["reason"] == "INDETERMINATE_ENTRY"
    assert r["R"] is None


def test_invariant_halt_entry_no_deferral(spec):
    r = simulate_intent(session(halts=[halt("13:04", "13:07")]), spec, intent(), "BASELINE")
    assert r["status"] == "NO_FILL"
    assert r["reason"] == "ENTRY_DUE_HALTED"


def test_golden_partial_overlap_c3(spec):
    r = simulate_intent(session(halts=[halt("13:11", "13:14")],
                               replacements={("DIA", at("13:10")): bar("13:10", lo="98")}), spec, intent(), "BASELINE")
    assert r["reason"] == "STOP"
    assert r["time_in_market_wall_minutes"] == 10
    assert r["time_in_market_tradable_minutes"] == 7


def test_invariant_full_halt_no_fill_reopening_stop(spec):
    s = session(halts=[halt("13:10", "13:20")],
                replacements={("DIA", at("13:10")): bar("13:10", lo="1"),
                              ("DIA", at("13:20")): bar("13:20", o="98", lo="97")})
    r = simulate_intent(s, spec, intent(), "BASELINE")
    assert r["reason"] == "GAP_THROUGH_STOP"
    assert r["execution_time_lower_bound"] == at("13:20").isoformat()
    assert r["time_in_market_tradable_minutes"] == 10


def test_invariant_missing_reopening(spec):
    r = simulate_intent(session(halts=[halt("13:10", "13:20")], omit={("DIA", at("13:20"))}), spec, intent(), "BASELINE")
    assert r["reason"] == "INDETERMINATE_POST_ENTRY"


def test_golden_halt_extends_holding_not_flat(spec):
    r = simulate_intent(session(halts=[halt("13:11", "13:14")]), spec, intent(), "BASELINE")
    assert r["accounting_exit_timestamp"] == at("13:40").isoformat()
    assert r["time_in_market_tradable_minutes"] == 32
    r = simulate_intent(session(close="14:25", halts=[halt("13:10", "13:35")]), spec, intent(), "BASELINE")
    assert r["reason"] == "FORCE_FLAT"
    assert r["accounting_exit_timestamp"] == at("13:40").isoformat()


def test_invariant_force_flat_missing_no_overnight(spec):
    r = simulate_intent(session(close="14:25", halts=[halt("13:10", "13:35")],
                               omit={("DIA", at("13:40"))}), spec, intent(), "BASELINE")
    assert r["status"] == "INDETERMINATE_FORCE_FLAT"
    assert r["R"] is None
    assert "accounting_exit_timestamp" not in r


def test_golden_controls_independent_paths(spec, monkeypatch):
    import trading_research.v2.simulator as simulator
    monkeypatch.setattr(simulator, "panel", lambda *args: (feature_panel(), None))
    s = session(replacements={("DIA", at("13:05")): bar("13:05", o="101", h="101"),
                              ("DIA", at("13:10")): bar("13:10", o="99.99")})
    normal = simulate_session(s, spec, scenario="STRESS")
    delayed = simulate_session(s, spec, scenario="STRESS", delay_diagnostic=True)
    control = simulate_session(s, spec, scenario="STRESS", control=True)
    assert normal["reason"] == "ENTRY_CAP"
    assert delayed["entry_timestamp"] == at("13:10").isoformat()
    assert delayed["entry"] == "100.09"
    assert control["symbol"] == "SPY"
    assert control["entry"] == "100.10"
    assert control["status"] == "SCORED"  # No canonical fill dependency.
    assert no_trade([s])[0]["daily_R"] == 0


def test_invariant_neighbor_controls_rejected(spec):
    with pytest.raises(SafetyError, match="CANONICAL"):
        simulate_session(session(), spec, variant="CSLC_L15", control=True)
    with pytest.raises(SafetyError, match="DIAGNOSTIC"):
        simulate_session(session(), spec, scenario="BASELINE", delay_diagnostic=True)


@pytest.mark.parametrize("ratio,regime", [(.0005, "LOW"), (.00050001, "NORMAL"), (.001, "NORMAL"), (.00100001, "HIGH")])
def test_golden_volatility_boundaries(spec, ratio, regime):
    i = intent()
    i["feature"]["atr"] = ratio * 100
    assert simulate_intent(session(), spec, i, "BASELINE")["volatility_regime"] == regime


def test_invariant_deterministic_replay(spec):
    s = session()
    assert simulate_intent(s, spec, intent(), "BASELINE") == simulate_intent(s, spec, intent(), "BASELINE")


def test_invariant_real_adapter_forbidden():
    with pytest.raises(SafetyError, match="REAL_DATA"):
        SyntheticSession(source="ALPACA", opened=at("09:30"), closed=at("16:00"), bars={})
