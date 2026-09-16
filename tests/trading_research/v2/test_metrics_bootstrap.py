import math

import numpy as np
import pytest

from trading_research.v2.metrics import core, quantiles, severe_tails, summarize, summarize_ledger, scored
from trading_research.v2.bootstrap import block_indices, lower_bound
from trading_research.v2.controls import evaluate_synthetic_sessions
from trading_runtime.config import SafetyError
from .conftest import SOURCE, record, session

PROBS = [.01, .05, .25, .5, .75, .95, .99]


@pytest.mark.parametrize("sample,expected", [
    ([0.0], [0.0]*7),
    ([0.0, 10.0], [.1, .5, 2.5, 5, 7.5, 9.5, 9.9]),
    ([], [None]*7),
])
def test_golden_type7(sample, expected):
    assert quantiles(sample, PROBS) == expected


def test_golden_type7_symmetric():
    q = quantiles([-2., -1., 0., 1., 2.], PROBS)
    assert (q[2], q[3], q[4]) == (-1, 0, 1)


@pytest.mark.parametrize("sample,counts", [
    ([-3,-2,-1,0,1,2,3,4,5,6], (10,2,1,5,4)),
    ([-2,-1,0,1,2], (5,1,0,1,0)),
    ([], (0,0,0,0,0)),
    ([0], (1,0,0,0,0)),
])
def test_golden_c4(sample, counts):
    result = severe_tails([record(x) for x in sample], stage="development",
                          variant="CSLC_L30", record_type="STRATEGY")
    assert tuple(result.values()) == counts
    assert all(type(v) is int for v in result.values())


@pytest.mark.parametrize("status,r,extra", [
    ("NO_TRADE", 0, {}), ("NO_SIGNAL", 0, {}), ("MASKED", -3, {}),
    ("INDETERMINATE", -3, {}), ("INDETERMINATE_FORCE_FLAT", -3, {}),
    ("SCORED", None, {}), ("SCORED", math.inf, {}),
    ("SCORED", math.nan, {}), ("SCORED", -3, {"integrity_violations": 1}),
])
def test_invariant_c4_sample_exclusions(status, r, extra):
    rows = [record(0), record(r, status=status, **extra)]
    tails = severe_tails(rows, stage="development", variant="CSLC_L30", record_type="STRATEGY")
    assert tails["scored_trade_count"] == 1
    assert tails["left_tail_lt_minus_1R"] == 0


@pytest.mark.parametrize("field,value", [("scenario","BASELINE"), ("scenario","STRESS"), ("stage","validation"),
                                         ("variant","CSLC_L15"), ("record_type","TIME_MATCHED_SPY")])
def test_invariant_c4_partition(field, value):
    row = record(3)
    row[field] = value
    with pytest.raises(SafetyError, match="PARTITION"):
        severe_tails([record(0), row], stage="development", variant="CSLC_L30", record_type="STRATEGY")


def test_golden_pf_dd_streak():
    rows = [record(r, time=f"13:{i*5:02}:00") for i, r in enumerate([-1,-2,0,-1,4])]
    m = core(rows)
    assert m["profit_factor"] == 1
    assert m["max_drawdown_R"] == 4
    assert m["longest_losing_streak"] == 2
    assert m["mean_R"] == 0
    assert m["cumulative_R"] == 0
    assert m["win_rate"] == .2
    assert m["zero_rate"] == .2
    assert m["loss_rate"] == .6
    assert m["average_loser_R"] == -4/3
    assert core([record(1), record(0)])["profit_factor"] is None
    assert core([record(-2)])["max_drawdown_R"] == 2
    assert core([])["mean_R"] is None
    assert core([record(None, status="INDETERMINATE_FORCE_FLAT")])["indeterminate"] == 1


def test_golden_concentration_and_removal(spec):
    rows = [record(3, symbol="DIA", day="2001-01-01"),
            record(2, symbol="IWM", day="2001-01-02"),
            record(-1, symbol="DIA", day="2001-02-01"),
            record(1, symbol="QQQ", day="2001-02-02")]
    m = summarize(rows, [r["session"] for r in rows], spec)
    assert m["best_symbol"] == "DIA"  # DIA and IWM tie at 2, ASCII resolves.
    assert m["best_month"] == "2001-01"
    assert m["positive_symbol_contribution_share"] == .4
    assert m["positive_month_contribution_share"] == 1
    assert m["positive_trade_contribution_share"] == .5
    assert m["exclude_best_symbol"]["mean_R"] == 1.5
    assert m["exclude_best_month"]["mean_R"] == 0
    assert m["exclude_best_trade"]["mean_R"] == 2/3


def test_golden_exit_order_uses_instants():
    later = record(-1, symbol="DIA", accounting_exit_timestamp="2001-02-05T18:15:00+00:00")
    earlier = record(2, symbol="IWM", accounting_exit_timestamp="2001-02-05T13:10:00-05:00")
    assert scored([later, earlier])[0] == earlier
    assert scored([record(1, symbol="SPY"), record(1, symbol="DIA")])[0]["symbol"] == "DIA"


def test_golden_scheduled_zero_days(spec):
    days = ["2001-02-05", "2001-02-06"]
    m = summarize([record(0)], days, spec)
    assert m["daily"][days[1]] == {"R": 0.0, "count": 0}
    assert m["daily"][days[0]] == {"R": 0.0, "count": 1}
    assert m["R_quantiles_01_05_25_50_75_95_99"] == [0]*7


def test_golden_severe_empty_summary(spec):
    m = summarize_ledger([], ["2001-02-05"], spec, stage="development", variant="CSLC_L30",
                         scenario="SEVERE", record_type="STRATEGY")
    assert m["tail_counts"] == dict.fromkeys(m["tail_counts"], 0)
    assert m["profit_factor"] is None


def test_golden_bootstrap_fixed_indices(spec):
    x = block_indices(10, spec)
    # Independent reference PCG64 seed starts: [[3,4],[5,3],[1,1]].
    assert x[:3].tolist() == [[3,4,5,6,7,4,5,6,7,8],
                             [5,6,7,8,9,3,4,5,6,7],
                             [1,2,3,4,5,1,2,3,4,5]]
    assert x.shape == (50000, 10)
    assert not x.flags.writeable


def test_invariant_bootstrap_order_and_replay(spec):
    a = block_indices(7, spec)
    block_indices(15, spec)
    assert np.array_equal(a, block_indices(7, spec))
    assert np.all((a >= 0) & (a < 7))
    assert np.all(np.diff(a[:, :5], axis=1) == 1)


def test_golden_bootstrap_zero_denominator(spec):
    daily = {str(d): {"R": 0., "count": 0} for d in range(5)}
    assert lower_bound(daily, "development", spec, source=SOURCE) == -math.inf
    daily["0"] = {"R": 2., "count": 1}
    assert lower_bound(daily, "development", spec, source=SOURCE) == 2


def test_golden_bootstrap_quantile_reference(spec):
    daily = {str(d): {"R": float(d-3), "count": 1} for d in range(10)}
    # Independent loop-based blocks, rather than production block_indices/statistic.
    rng = np.random.Generator(np.random.PCG64(20260915))
    values = []
    for _ in range(50000):
        starts = rng.integers(0, 6, size=2, dtype=np.int64)
        total = sum(sum(float(j-3) for j in range(int(s), int(s)+5)) for s in starts)
        values.append(total/10)
    values.sort()
    expected = values[math.ceil(50000 * .016666666666666666)-1]
    assert lower_bound(daily, "development", spec, source=SOURCE) == expected
    assert expected == -1.0


def test_invariant_bootstrap_invalid_count(spec):
    d = {str(x): {"R": 0., "count": 0} for x in range(5)}
    d["0"]["count"] = .5
    with pytest.raises(SafetyError):
        lower_bound(d, "development", spec, source=SOURCE)


def test_invariant_second_entry_day(spec):
    with pytest.raises(SafetyError, match="SECOND_ENTRY"):
        evaluate_synthetic_sessions([session(), session()], spec)


def test_invariant_controls_empty_and_synthetic(spec):
    m = evaluate_synthetic_sessions([session()], spec, scenario="SEVERE")["summary"]
    assert m["tail_counts"]["scored_trade_count"] == 0
    with pytest.raises(SafetyError, match="REAL_DATA"):
        core([{"source": "ALPACA", "status": "SCORED", "R": 2}])
