from datetime import timedelta
from decimal import Decimal
import math

import pytest

from trading_research.v2.features import dispersion, median13, panel
from trading_research.v2.models import SyntheticSession
from trading_research.v2.signal import build_intent
from trading_research.v2_protocol import UNIVERSE
from trading_runtime.config import SafetyError
from .conftest import SOURCE, at, bar, session, halt, feature_panel


def analytical_session(**kwargs):
    changes = {}
    for i, symbol in enumerate(UNIVERSE):
        close = Decimal("99") if i == 0 else Decimal("100") + Decimal(i) / 10
        prev = close - Decimal(".10") if i == 0 else close
        for time, c in (("12:50", prev), ("12:55", close)):
            changes[symbol, at(time)] = bar(time, o=str(c), h=str(c + Decimal(".10")),
                                          lo=str(c - Decimal(".10")), c=str(c))
    changes.update(kwargs.pop("replacements", {}))
    return session(replacements=changes, **kwargs)


def test_golden_median_mad_z():
    moves = dict(zip(UNIVERSE, range(-6, 7)))
    m, mad, zs = dispersion(moves, 1.4826)
    assert m == 0
    assert mad == 3
    assert zs["DIA"] == pytest.approx(-1.3489815189531904)
    assert zs["XLF"] == 0
    assert median13(list(reversed(range(13)))) == 6


def test_golden_mad_zero(spec):
    assert dispersion(dict.fromkeys(UNIVERSE, .1), 1.4826) is None
    assert panel(session(), spec, "CSLC_L30")[1] == "NO_SIGNAL_MAD_ZERO"


@pytest.mark.parametrize("variant", ["CSLC_L15", "CSLC_L30", "CSLC_L45"])
def test_golden_features(spec, variant):
    s = analytical_session()
    p, reason = panel(s, spec, variant)
    assert reason is None
    assert p["symbols"]["DIA"]["move"] == math.log(99/100)
    assert p["symbols"]["DIA"]["relative_turn"] == math.log(99/98.9)
    # Ten ranges .2, then a 1.2 gap true range, then .2 = 3.4/12.
    assert p["symbols"]["DIA"]["atr"] == pytest.approx(3.4 / 12)
    assert p["symbols"]["DIA"]["dollar_volume"] == pytest.approx((40*100 + 98.9 + 99)*100000)
    assert sorted(p["symbols"], key=lambda x: p["symbols"][x]["z"])[0] == "DIA"


@pytest.mark.parametrize("symbol", UNIVERSE)
def test_invariant_all_thirteen_required(spec, symbol):
    s = analytical_session(omit={(symbol, at("12:20"))})
    assert panel(s, spec, "CSLC_L30")[1] == "NO_SIGNAL_MISSING_PANEL"


def test_invariant_masked_input_blocks(spec):
    s = analytical_session(invalid_slots={("XLY", at("12:10"))})
    assert panel(s, spec, "CSLC_L30")[0] is None
    assert panel(analytical_session(excluded=["QQQ"]), spec, "CSLC_L30")[1] == "NO_SIGNAL_QC_EXCLUDED"


def test_invariant_halt_feature_window(spec):
    assert panel(analytical_session(halts=[halt("12:00", "12:01")]), spec, "CSLC_L30")[1] == "NO_SIGNAL_FEATURE_HALT"
    assert panel(analytical_session(halts=[halt("14:00", "14:01")]), spec, "CSLC_L30")[1] is None


def test_invariant_no_future_visible(spec):
    s = analytical_session()
    future = analytical_session(replacements={("DIA", at("13:00")): bar("13:00", o="200", h="200", lo="200", c="200")})
    assert panel(s, spec, "CSLC_L30") == panel(future, spec, "CSLC_L30")
    assert all(b.end <= at("13:00") for b in s.visible("DIA", at("13:00")))


def test_invariant_no_cross_day():
    with pytest.raises(SafetyError, match="CROSS_DAY"):
        SyntheticSession(source=SOURCE, opened=at("09:30"), closed=at("16:00")+timedelta(days=1), bars={})
    with pytest.raises(SafetyError, match="SESSION_BAR"):
        SyntheticSession(source=SOURCE, opened=at("09:30"), closed=at("16:00"), bars={"DIA": [bar(at("12:00")-timedelta(days=1))]})


def test_invariant_input_order(spec):
    s = analytical_session()
    reversed_session = SyntheticSession(source=SOURCE, opened=s.open, closed=s.close,
                                        bars={k: list(reversed(list(v.values()))) for k, v in reversed(list(s.bars.items()))})
    assert panel(s, spec, "CSLC_L30") == panel(reversed_session, spec, "CSLC_L30")


def test_golden_ascii_rank_and_no_fallback(spec):
    p = feature_panel()
    p["symbols"]["IWM"]["z"] = -2
    assert build_intent(p, session(), spec, 5)[0]["symbol"] == "DIA"
    p["symbols"]["DIA"]["relative_turn"] = 0
    assert build_intent(p, session(), spec, 5) == (None, "NO_SIGNAL_RELATIVE_TURN")


@pytest.mark.parametrize("field,value,reason", [
    ("z", -.999, "NO_SIGNAL_Z"), ("relative_turn", 0, "NO_SIGNAL_RELATIVE_TURN"),
    ("close", 4.999, "NO_SIGNAL_PRICE_VOLUME"), ("close", 1000.001, "NO_SIGNAL_PRICE_VOLUME"),
    ("dollar_volume", 9999999.99, "NO_SIGNAL_PRICE_VOLUME"),
    ("atr", 1.01, "NO_SIGNAL_RISK"),
])
def test_golden_entry_filters(spec, field, value, reason):
    p = feature_panel()
    p["symbols"]["DIA"][field] = value
    assert build_intent(p, session(), spec, 5)[1] == reason


def test_golden_common_state(spec):
    p = feature_panel()
    p["median"] = 0
    assert build_intent(p, session(), spec, 5)[1] == "NO_SIGNAL_COMMON_STATE"
    p["median"] = .1
    for s in ("SPY", "QQQ"):
        p["symbols"][s]["move"] = 0
    assert build_intent(p, session(), spec, 5)[1] == "NO_SIGNAL_COMMON_STATE"


def test_golden_early_close_and_strict_time(spec):
    assert build_intent(feature_panel(), session(close="13:00"), spec, 5)[1] == "NO_SIGNAL_TIME"
    # 13:05 +30 == 14:20 -45: equality is rejected.
    assert build_intent(feature_panel(), session(close="14:20"), spec, 5)[1] == "NO_SIGNAL_TIME"
    assert build_intent(feature_panel(), session(close="14:25"), spec, 5)[0] is not None


def test_invariant_copied_inputs_immutable():
    s = analytical_session()
    with pytest.raises(TypeError):
        s.bars["DIA"][at("12:00")] = bar("12:00")


def test_invariant_nonfinite_features_fail_closed(spec):
    huge = bar("12:55", o="1e999", h="1e999", lo="1e999", c="1e999")
    assert panel(analytical_session(replacements={("DIA", at("12:55")): huge}), spec, "CSLC_L30")[1] == "NO_SIGNAL_NONFINITE"
