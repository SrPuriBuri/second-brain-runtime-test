from decimal import Decimal, getcontext, localcontext
import math

import numpy as np
import pytest

from trading_research.v2.numeric import decimal_from_f64, exact_decimal, floor_cent, ceil_cent, money_context
from trading_research.v2.fills import risk_prices, entry_price, exit_price, size_limits, enter, net_r, protective
from trading_runtime.config import SafetyError
from .conftest import bar, intent


def test_golden_c1(spec):
    f = {"close": 100.0, "close_text": "100", "atr": .1}
    assert decimal_from_f64(np.float64(.2)) == Decimal("0.2")
    assert risk_prices(f, spec.base["simulation"]["signal"]["parameters"]) == (Decimal("99.80"), Decimal("100.40"))
    # Independent counterexample: direct binary conversion is explicitly different.
    with localcontext() as ctx:
        ctx.prec = 34
        wrong = floor_cent(Decimal("100") - Decimal.from_float(.2))
    assert wrong == Decimal("99.79")


@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf])
def test_invariant_nonfinite_rejected(value):
    with pytest.raises(SafetyError):
        decimal_from_f64(value)


@pytest.mark.parametrize("value", [100.125, Decimal("100.125"), "NaN", "Infinity", "not a price"])
def test_invariant_exact_source_only(value):
    with pytest.raises(SafetyError):
        exact_decimal(value)


def test_golden_source_decimal_not_binary():
    assert exact_decimal("100.123456789012345678901234") == Decimal("100.123456789012345678901234")
    assert exact_decimal("100.123456789012345678901234") != decimal_from_f64(100.123456789012345678901234)


@pytest.mark.parametrize("value,floor,ceil", [("100.001", "100.00", "100.01"), ("-0.001", "-0.01", "-0.00"), ("100.10", "100.10", "100.10")])
def test_golden_cent(value, floor, ceil):
    assert floor_cent(Decimal(value)) == Decimal(floor)
    assert ceil_cent(Decimal(value)) == Decimal(ceil)


def test_invariant_context_independence(spec):
    original = getcontext().copy()
    with localcontext() as ambient:
        ambient.prec = 3
        with money_context() as ctx:
            assert ctx.prec == 34
            assert str(ctx.rounding) == "ROUND_HALF_EVEN"
        assert entry_price(Decimal("100.001"), spec.base["costs"]["BASELINE"]) == Decimal("100.06")
    assert getcontext() == original or getcontext().prec == original.prec


@pytest.mark.parametrize("scenario,entry,exit", [("BASELINE", "100.05", "99.94"), ("STRESS", "100.10", "99.89"), ("SEVERE", "100.20", "99.79")])
def test_golden_scenario_prices(spec, scenario, entry, exit):
    costs = spec.base["costs"][scenario]
    assert entry_price(Decimal("100"), costs) == Decimal(entry)
    assert exit_price(Decimal("100"), costs) == Decimal(exit)


def test_golden_quantity_and_net_r(spec):
    position, reason = enter(intent(), bar("13:05"), spec.base["costs"]["BASELINE"], spec.base["simulation"]["sizing"])
    assert reason is None
    assert position["quantity"] == 24  # floor(2500/100.05), other caps exceed this.
    assert net_r(position, Decimal("98.74")) == -1.048  # (98.74-100.05)/(100.05-98.80)


@pytest.mark.parametrize("entry,stop,volume,limiting,expected", [
    ("100", "98", "100000", "risk", "25"),
    ("100", "99", "100000", "notional", "25"),
    ("100", "99", "100000", "cash", "100"),
    ("1", "0.99", "1000000", "quantity", "1000"),
    ("100", "99", "30", "volume", "0.30"),
])
def test_golden_each_sizing_component(spec, entry, stop, volume, limiting, expected):
    values = size_limits(Decimal(entry), Decimal(stop), Decimal(volume), spec.base["simulation"]["sizing"])
    assert values[limiting] == Decimal(expected)


@pytest.mark.parametrize("opened,reason", [("100.30", "ENTRY_CAP"), ("98.80", "ENTRY_PRICE_RISK"), ("100.20", "ENTRY_RR")])
def test_golden_entry_rejection(spec, opened, reason):
    i = intent()
    if reason == "ENTRY_RR":
        i["target"] = Decimal("101.00")
    b = bar("13:05", o=opened, h="101", lo="98")
    assert enter(i, b, spec.base["costs"]["BASELINE"], spec.base["simulation"]["sizing"])[1] == reason


def test_golden_volume_zero_quantity(spec):
    i = intent()
    i["feature"]["last_volume_text"] = "1"
    assert enter(i, bar("13:05"), spec.base["costs"]["BASELINE"], spec.base["simulation"]["sizing"])[1] == "ENTRY_ZERO_QUANTITY"


@pytest.mark.parametrize("atr,expected", [(1.0, (Decimal("98.00"), Decimal("104.00"))), (1.00001, None), (0.0, (Decimal("99.80"), Decimal("100.40")))])
def test_golden_risk_boundary(spec, atr, expected):
    assert risk_prices({"close": 100.0, "close_text": "100", "atr": atr}, spec.base["simulation"]["signal"]["parameters"]) == expected


@pytest.mark.parametrize("o,h,lo,expected", [
    ("98", "103", "97", (Decimal("98"), "GAP_THROUGH_STOP")),
    ("100", "103", "98.8", (Decimal("98.80"), "STOP")),
    ("100", "102.4", "99", None),
    ("100", "102.41", "99", (Decimal("102.40"), "TARGET")),
])
def test_golden_protective_prices(o, h, lo, expected):
    assert protective(intent(), bar("13:05", o=o, h=h, lo=lo)) == expected
