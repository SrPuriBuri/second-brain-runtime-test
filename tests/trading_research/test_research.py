import ast
from copy import deepcopy
from dataclasses import replace
from datetime import date, timedelta
import json
from pathlib import Path
import subprocess
import sys

import pytest

from trading_research.bars import Bar, Dataset
from trading_research.candidates import Variant, grid, signal
from trading_research.costs import Costs
from trading_research.data import digest
from trading_research.features import build_features, visible_news
from trading_research.metrics import core, summarize, bootstrap_lower, performance_gates
from trading_research.simulator import simulate, exit_assumption
from trading_research.splits import sample_days, validate_protocol, claim_local_oos
from trading_runtime.config import SafetyError
from trading_runtime.market_calendar import session_from_row, display_time
from trading_runtime.models import Policy


@pytest.fixture
def protocol():
    # Synthetic test policy, not a frozen operational strategy.
    return {
        "version": 1,
        "symbols": ["SPY", "QQQ"],
        "feed": "iex",
        "adjustment": "raw",
        "warmup_start": "2023-01-01",
        "development": ["2024-01-01", "2024-12-31"],
        "validation": ["2025-01-01", "2025-12-31"],
        "oos": ["2026-01-01", "2026-08-31"],
        "thresholds": {
            "opening_momentum": [0, 0.001, 0.002],
            "relative_strength": [0.001, 0.002, 0.003],
            "gap_continuation": [0.005, 0.01, 0.015],
            "mean_reversion": [0.005, 0.01, 0.015],
        },
        "decision_offsets": [60, 150],
        "delay_minutes": 5,
        "force_flat_minutes": 45,
        "initial_cash": 10000,
        "history_sessions": 20,
        "rvol_threshold": 1.25,
        "stop_buffer_fraction": 0.001,
        "min_stop_fraction": 0.002,
        "max_stop_fraction": 0.02,
        "target_r": 2,
        "max_entry_premium": 0.001,
        "bar_participation_cap": 0.01,
        "costs": {
            "baseline": {"half_spread_bps": 2, "slippage_bps": 3, "exit_fee_bps": 1},
            "stress": {"half_spread_bps": 4, "slippage_bps": 6, "exit_fee_bps": 1},
            "severe": {"half_spread_bps": 8, "slippage_bps": 12, "exit_fee_bps": 1},
        },
        "gates": {
            "development_trades": 200,
            "validation_trades": 100,
            "oos_trades": 200,
            "expectancy_r": 0.1,
            "profit_factor": 1.2,
            "stress_profit_factor": 1.05,
            "max_drawdown_r": 20,
            "max_losing_streak": 12,
            "neighbor_trades": 100,
            "positive_neighbors": 2,
            "symbol_positive_share": 0.65,
            "month_positive_share": 0.3,
            "oos_months": 12,
        },
        "corporate_actions_verified": False,
        "bootstrap_resamples": 50,
        "bootstrap_seed": 731,
    }


@pytest.fixture
def bundle():
    calendar = []
    day = date(2024, 1, 2)
    # Explicit synthetic weekday sessions; not an exchange calendar fixture claim.
    while len(calendar) < 23:
        if day.weekday() < 5:
            calendar.append({"date": str(day), "open": "09:30", "close": "16:00"})
        day += timedelta(days=1)
    bars = {s: [] for s in ("SPY", "QQQ")}
    for row in calendar:
        session = session_from_row(row)
        for symbol in bars:
            for i in range(78):
                bars[symbol].append(
                    {
                        "t": (session.open + timedelta(minutes=5 * i)).isoformat(),
                        "o": 100,
                        "h": 100.2,
                        "l": 99.8,
                        "c": 100,
                        "v": 100000,
                    }
                )
    return {
        "source": "synthetic_fixture",
        "feed": "iex",
        "adjustment": "raw",
        "asof": "-",
        "timeframe": "5Min",
        "calendar": calendar,
        "bars": bars,
    }


def test_chronological_split_and_oos_seal(bundle, protocol):
    dataset = Dataset(bundle)
    assert len(sample_days(dataset, protocol, "development")) == 23
    assert sample_days(dataset, protocol, "validation") == []
    with pytest.raises(SafetyError, match="OOS_SEALED"):
        sample_days(dataset, protocol, "oos")
    protocol["validation"][0] = "2024-12-31"
    with pytest.raises(SafetyError, match="OVERLAPPING"):
        validate_protocol(protocol)


def test_oos_claim_is_exclusive_and_bound(tmp_path, protocol):
    selection = {
        "selected_variant": "fixture",
        "protocol_hash": digest(protocol),
        "dataset_hash": "original",
    }
    with pytest.raises(SafetyError, match="MISMATCH"):
        claim_local_oos(tmp_path, selection, protocol, "modified")
    claim_local_oos(tmp_path, selection, protocol, "original")
    with pytest.raises(SafetyError, match="CONSUMED"):
        claim_local_oos(tmp_path, selection, protocol, "original")


def test_future_bar_and_partial_bar_not_visible(bundle):
    dataset = Dataset(bundle)
    day = dataset.days[-1]
    at = dataset.sessions[day].open + timedelta(minutes=60)
    before = build_features(dataset, "SPY", day, 60)
    changed = deepcopy(bundle)
    for row in changed["bars"]["SPY"]:
        if row["t"] >= at.isoformat():
            row.update({"o": 900, "h": 999, "l": 800, "c": 950, "v": 1})
    other = Dataset(changed)
    assert before == build_features(other, "SPY", day, 60)
    assert all(b.end <= at for b in other.window("SPY", day, at))


def test_volume_uses_same_elapsed_past_window(bundle):
    dataset = Dataset(bundle)
    f = build_features(dataset, "SPY", dataset.days[-1], 60)
    assert f.rvol == 1
    changed = deepcopy(bundle)
    last = dataset.days[-1]
    for row in changed["bars"]["SPY"]:
        if row["t"].startswith(last) and "T15:" in row["t"]:
            row["v"] = 100000000
    assert build_features(Dataset(changed), "SPY", last, 60).rvol == 1


def test_duplicate_rejected_and_missing_not_filled(bundle):
    duplicate = deepcopy(bundle)
    duplicate["bars"]["SPY"].append(duplicate["bars"]["SPY"][0])
    with pytest.raises(SafetyError, match="DUPLICATE_BAR"):
        Dataset(duplicate)
    del bundle["bars"]["SPY"][-78]
    dataset = Dataset(bundle)
    assert build_features(dataset, "SPY", dataset.days[-1], 60) is None
    assert dataset.audit()["coverage"]["SPY"]["missing_bars"] == 1


@pytest.mark.parametrize(
    "patch",
    [{"o": 0}, {"h": 90}, {"v": 0}, {"c": float("nan")}, {"t": "2024-01-02T09:30:00"}],
)
def test_invalid_bar_rejected(bundle, patch):
    with pytest.raises((SafetyError, ValueError)):
        Bar.parse({**bundle["bars"]["SPY"][0], **patch})


def test_adjusted_input_not_mixed_with_raw_and_split_jump_no_trade(bundle):
    adjusted = deepcopy(bundle)
    adjusted["adjustment"] = "split"
    with pytest.raises(SafetyError, match="PROVENANCE"):
        Dataset(adjusted)
    last = bundle["calendar"][-1]["date"]
    for row in bundle["bars"]["SPY"]:
        if row["t"].startswith(last):
            for k in ("o", "h", "l", "c"):
                row[k] /= 2
    assert build_features(Dataset(bundle), "SPY", last, 60) is None


def test_actual_session_boundaries_holiday_and_dst(bundle):
    for day, expected in [("2026-03-16", "T14:30:00"), ("2026-09-10", "T15:30:00")]:
        s = session_from_row({"date": day, "open": "09:30", "close": "16:00"})
        assert expected in display_time(s.open)["Europe/Madrid"]
    bundle["calendar"] = bundle["calendar"][1:]  # Explicitly absent session.
    dataset = Dataset(bundle)
    assert dataset.outside_session == 156
    assert dataset.days[0] != "2024-01-02"


def test_costs_and_gap_and_ambiguous_bar():
    c = Costs(2, 3, 1)
    assert c.entry(100) == pytest.approx(100.05)
    assert c.exit(100) == pytest.approx(99.94)
    row = {
        "t": "2024-01-02T10:35:00-05:00",
        "o": 100,
        "h": 104,
        "l": 97,
        "c": 101,
        "v": 1000,
    }
    p = {"stop": 98, "target": 103}
    assert exit_assumption(p, Bar.parse(row)) == (98, "STOP")
    assert exit_assumption(p, Bar.parse({**row, "o": 97})) == (97, "GAP_THROUGH_STOP")
    assert exit_assumption(p, Bar.parse({**row, "h": 103, "l": 99})) is None
    assert exit_assumption(p, Bar.parse({**row, "h": 105, "o": 104, "l": 99})) == (
        103,
        "TARGET",
    )


def force_setup(monkeypatch):
    def fixture_setup(*args):
        return {
            "side": "BUY",
            "reference": 100,
            "stop": 99,
            "target": 102,
            "planned_risk": 1,
        }

    monkeypatch.setattr("trading_research.simulator.signal", fixture_setup)


def test_entry_delay_cash_caps_force_flat_no_overnight(bundle, protocol, monkeypatch):
    force_setup(monkeypatch)
    dataset = Dataset(bundle)
    sim = simulate(
        dataset,
        dataset.days[-1:],
        Variant("opening_momentum", 0, False, False),
        protocol,
        Policy(),
        Costs(2, 3, 1),
    )
    assert len(sim["trades"]) == 2
    assert sim["remaining_positions"] == 0
    for t in sim["trades"]:
        assert "T10:35:00" in t["entry_at"] and "T15:15:00" in t["exit_at"]
        assert t["reason"] == "FORCE_FLAT" and t["side"] == "BUY"
        assert t["quantity"] == 24  # floor(2500/100.05), no margin dependence
        assert t["r"] == pytest.approx(-0.11)
    assert (
        simulate(
            dataset,
            dataset.days[-1:],
            Variant("opening_momentum", 0, False, False),
            protocol,
            Policy(),
            Costs(2, 3, 1),
        )
        == sim
    )


def test_early_close_forced_exit(bundle, protocol, monkeypatch):
    force_setup(monkeypatch)
    bundle["calendar"][-1]["close"] = "13:00"
    dataset = Dataset(bundle)
    sim = simulate(
        dataset,
        dataset.days[-1:],
        Variant("opening_momentum", 0, False, False),
        protocol,
        Policy(),
        Costs(2, 3, 1),
    )
    assert sim["trades"] and all("T12:15:00" in t["exit_at"] for t in sim["trades"])


def test_future_missing_exit_is_indeterminate_not_deleted(
    bundle, protocol, monkeypatch
):
    force_setup(monkeypatch)
    last = bundle["calendar"][-1]["date"]
    bundle["bars"]["SPY"] = [
        b
        for b in bundle["bars"]["SPY"]
        if not (b["t"].startswith(last) and "T15:15:00" in b["t"])
    ]
    dataset = Dataset(bundle)
    sim = simulate(
        dataset,
        [last],
        Variant("opening_momentum", 0, False, False),
        protocol,
        Policy(),
        Costs(2, 3, 1),
    )
    unknown = [t for t in sim["trades"] if t["symbol"] == "SPY"]
    assert len(unknown) == 1 and unknown[0]["r"] is None
    metric = summarize(sim)
    assert metric["trade_count"] == 2 and metric["indeterminate"] == 1
    assert "INDETERMINATE_OUTCOMES" in performance_gates(
        metric, metric, protocol, "development"
    )


def test_missing_signal_data_no_trade(bundle, protocol):
    dataset = Dataset(bundle)
    sim = simulate(
        dataset,
        dataset.days[:2],
        Variant("opening_momentum", 0, False, False),
        protocol,
        Policy(),
        Costs(2, 3, 1),
    )
    assert not sim["trades"]
    assert sim["rejections"]["INVALID_OR_MISSING_PAST_DATA"] > 0


def trade(r, index):
    return {
        "r": r,
        "day": f"2024-01-{index + 2:02}",
        "symbol": "SPY",
        "entry_at": f"2024-01-{index + 2:02}T10:35:00-05:00",
        "exit_at": f"2024-01-{index + 2:02}T15:15:00-05:00",
        "minutes": 280,
        "regime": "favorable",
        "entry_bucket": "60",
    }


def test_metrics_hand_calculated_and_zero_denominators():
    trades = [trade(r, i) for i, r in enumerate([-1, 2, -2, -1, 1])]
    m = core(trades)
    assert m["expectancy_r"] == -0.2 and m["median_r"] == -1
    assert m["profit_factor"] == 0.75 and m["max_drawdown_r"] == 3
    assert m["longest_losing_streak"] == 2 and m["win_rate"] == 0.4
    assert core([])["profit_factor"] is None
    full = summarize(
        {"trades": trades, "sessions": 10, "session_minutes": 3900, "rejections": {}}
    )
    assert full["no_trade_days_pct"] == 50
    assert full["excluding_top_trade"]["cumulative_r"] == -3
    assert bootstrap_lower(
        trades, [t["day"] for t in trades], 50, 731
    ) == bootstrap_lower(trades, [t["day"] for t in trades], 50, 731)


def test_grid_and_pure_long_signals(bundle, protocol):
    assert len(grid(protocol)) == 48 and grid(protocol) == grid(deepcopy(protocol))
    dataset = Dataset(bundle)
    f = build_features(dataset, "SPY", dataset.days[-1], 60)
    bullish = replace(
        f,
        close=101,
        opening_high=100.5,
        recent_low=100,
        vwap=102,
        intraday_return=0.01,
        relative_return=0.01,
        rising=True,
        high=101,
        gap=0.02,
    )
    for v in grid(protocol):
        a, b = signal(bullish, v, protocol, 1.5), signal(bullish, v, protocol, 1.5)
        assert a == b
        assert a is None or a["side"] == "BUY"


def test_news_before_publication_or_revision_is_not_visible():
    at = session_from_row(
        {"date": "2024-01-02", "open": "10:30", "close": "16:00"}
    ).open
    items = [
        {
            "created_at": "2024-01-02T10:00:00-05:00",
            "updated_at": "2024-01-02T11:00:00-05:00",
        },
        {
            "created_at": "2024-01-02T11:00:00-05:00",
            "updated_at": "2024-01-02T11:00:00-05:00",
        },
        {
            "created_at": "2024-01-02T10:00:00-05:00",
            "updated_at": "2024-01-02T10:01:00-05:00",
        },
    ]
    assert visible_news(items, at) == [items[2]]


def test_no_execution_imports_or_mutation_calls():
    root = Path(__file__).resolve().parents[2]
    forbidden = {
        "alpaca_client",
        "executor",
        "runner",
        "simulation",
        "reconcile",
        "submit_order",
        "submit_bracket",
        "cancel_order",
        "close_owned",
        "close_all_positions",
        "replace_order",
    }
    for file in (root / "trading_research").rglob("*.py"):
        tree = ast.parse(file.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                assert not forbidden.intersection((node.module or "").split("."))
            if isinstance(node, ast.Import):
                assert all(
                    not forbidden.intersection(a.name.split(".")) for a in node.names
                )
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                assert node.func.attr not in forbidden
    script = "import trading_research.cli, sys; assert not any(m in sys.modules for m in ['trading_runtime.executor','trading_runtime.alpaca_client','trading_runtime.runner'])"
    subprocess.run(
        [sys.executable, "-c", script], cwd=root, check=True, capture_output=True
    )


def test_cli_missing_dataset_does_not_claim_backtest(capsys):
    from trading_research.cli import main

    assert main(["run-all"]) == 1
    assert (
        json.loads(capsys.readouterr().out)["reason"] == "HISTORICAL_DATASET_REQUIRED"
    )


def test_full_search_keeps_oos_unopened_and_accounts_for_trials(
    bundle, protocol, monkeypatch
):
    import trading_research.experiment as experiment
    import trading_research.walkforward as wf

    called_days = []
    real_simulate = simulate

    def capture(dataset, days, *args, **kwargs):
        called_days.extend(days)
        assert all(d < protocol["oos"][0] for d in days)
        return real_simulate(dataset, days, *args, **kwargs)

    monkeypatch.setattr(experiment, "simulate", capture)
    monkeypatch.setattr(wf, "simulate", capture)
    result = experiment.select(Dataset(bundle), protocol, Policy())
    assert result["decision"] == "NO_GO" and result["selected_variant"] is None
    assert result["oos_status"] == "SEALED"
    assert result["trial_count"]["parameter_filter_variants"] == 48
    assert result["trial_count"]["development_cost_evaluations"] == 144
    assert result["trial_count"]["walkforward_training_evaluations"] == 192
    assert len(result["development_rejections"]) == 48
    assert called_days
    assert all(
        m["baseline"]["trade_count"] == 0 for m in result["development"].values()
    )


def test_oos_rejects_changed_implementation_and_policy(bundle, protocol, tmp_path):
    from trading_research.experiment import (
        run_oos,
        implementation_commit,
        implementation_hash,
    )

    dataset = Dataset(bundle)
    selection = {"implementation_commit": "changed"}
    with pytest.raises(SafetyError, match="IMPLEMENTATION_CHANGED"):
        run_oos(dataset, protocol, Policy(), selection, tmp_path)
    selection.update(
        {
            "implementation_commit": implementation_commit(),
            "implementation_hash": implementation_hash(),
            "policy_hash": "modified",
        }
    )
    with pytest.raises(SafetyError, match="POLICY_CHANGED"):
        run_oos(dataset, protocol, Policy(), selection, tmp_path)
    assert not list(tmp_path.iterdir())


def test_result_write_preserves_finalized_artifact_on_invalid_output(tmp_path):
    from trading_research.experiment import save_json

    path = tmp_path / "result.json"
    save_json(path, {"complete": True})
    original = path.read_bytes()
    with pytest.raises(ValueError):
        save_json(path, {"partial": float("nan")})
    assert path.read_bytes() == original
    assert not list(tmp_path.glob("*.partial"))


def test_source_retry_budget_and_safe_error(tmp_path):
    import httpx
    from trading_runtime.config import Config
    from trading_research.data import HistoricalSource

    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(429, json={"message": "synthetic-secret-never-output"})

    source = HistoricalSource(
        Config("synthetic", "synthetic"),
        tmp_path,
        httpx.Client(transport=httpx.MockTransport(handler)),
        lambda _: None,
    )
    with pytest.raises(SafetyError, match="HISTORY_HTTP_429") as error:
        source.get("bars", {"feed": "iex"})
    assert len(calls) == 3 and "synthetic-secret" not in str(error.value)


def test_cloud_research_writes_only_derived_evidence_and_keeps_oos_sealed(
    bundle, protocol, tmp_path, monkeypatch
):
    from trading_research.cloud import run_cloud
    from trading_runtime.simulation import simulation_store

    store, github = simulation_store()
    github.seed(
        "state/readiness.json", {"execution_enabled": False, "execution_ready": False}
    )
    github.seed("research/protocol-v1.json", protocol)
    github.seed("research/RESEARCH_PROTOCOL_V1.md", "Synthetic preregistration fixture")
    monkeypatch.setenv("GITHUB_RUN_ID", "fixture")

    class Source:
        receipts = [{"route": "synthetic_fixture"}]
        requests = 0

        def get(self, route, params):
            assert route == "calendar"
            return bundle["calendar"]

        def bars(self, symbols, start, end):
            return bundle["bars"]

    result = run_cloud(Source(), store, tmp_path)
    assert result["decision"] == "NO_GO" and result["oos_status"] == "SEALED"
    assert (
        result["orders_submitted"]
        == result["orders_cancelled"]
        == result["positions_closed"]
        == 0
    )
    assert all(
        path.startswith("projects/ai-stock-trader/data/evidence/research-v1/")
        for path, _ in github.writes
    )
    assert all("[skip ci]" in payload["message"] for _, payload in github.writes)
    assert len(github.writes) == 50  # 48 variant records, selection, final manifest.
    assert not any("/oos_" in path for path, _ in github.writes)
