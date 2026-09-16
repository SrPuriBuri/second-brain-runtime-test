"""GENERATED_PROVENANCE_FIXTURE: dates are labels, prices are constructed in memory."""

from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timedelta
from pathlib import Path
import subprocess

import pytest

from trading_research.v2.bootstrap import lower_bound
from trading_research.v2.controls import evaluate_synthetic_sessions
from trading_research.v2.features import panel
from trading_research.v2.gates import evaluate
from trading_research.v2.ledger import Ledger, schedule, trial_identity
from trading_research.v2.metrics import summarize_ledger
from trading_research.v2.models import (
    HistoricalDevelopmentSession, NY, SyntheticSession, require_research_session, require_synthetic,
)
from trading_research.v2.provenance import GENERATED_FIXTURE, Provenance, require_provenance
from trading_research.v2.runner import require_command
from trading_research.v2.signal import build_intent
from trading_research.v2.simulator import simulate_intent, simulate_session, no_trade
from trading_runtime.config import SafetyError
from v2.conftest import intent, record, halt
from v2.test_features_signal import analytical_session
from v2.test_gates_ledger import identity, passing_bundle

S = Provenance.SYNTHETIC.value
H = Provenance.DEVELOPMENT.value


def generated(mode, day="2018-01-04", **kwargs):
    original = analytical_session(**kwargs)
    delta = datetime.fromisoformat(day).date() - original.open.date()
    def move(t):
        return t + delta
    cls = SyntheticSession if mode == S else HistoricalDevelopmentSession
    return cls(source=mode, stage="development", fixture_kind=GENERATED_FIXTURE,
               opened=move(original.open), closed=move(original.close),
               bars={s: [replace(b, start=move(b.start)) for b in rows.values()]
                     for s, rows in original.bars.items()},
               halts=[dict(start=move(a), end=move(b), verified=True,
                           source_ref=GENERATED_FIXTURE) for a, b in original.halts],
               excluded=original.excluded,
               invalid_slots=[(s, move(t)) for s, t in original.invalid_slots])


def economic(value):
    if isinstance(value, dict):
        return {k: economic(v) for k, v in value.items() if k not in {"source", "stage", "fixture_kind"}}
    if isinstance(value, (list, tuple)):
        return [economic(v) for v in value]
    return value


def generated_record(r, mode=H, **kwargs):
    return record(r, day="2018-01-04", source=mode, fixture_kind=GENERATED_FIXTURE, **kwargs)


def summary(rows, spec, mode):
    return summarize_ledger(rows, ["2018-01-04"], spec, stage="development",
                            variant="CSLC_L30", scenario="SEVERE", record_type="STRATEGY", source=mode)


def test_synthetic_still_supported():
    s = generated(S)
    require_synthetic(s)
    assert require_research_session(s) is Provenance.SYNTHETIC


@pytest.mark.parametrize("day", ["2016-01-04", "2021-12-31"])
def test_historical_inclusive_boundaries(day):
    assert require_research_session(generated(H, day)) is Provenance.DEVELOPMENT


@pytest.mark.parametrize("day", ["2016-01-03", "2022-01-01", "2024-01-02", "2025-01-02"])
def test_date_firewall_before_bar_iteration(day):
    class ForbiddenRows:
        def items(self):
            raise AssertionError("Forbidden date must reject before iterating input")
    opened = datetime.fromisoformat(day + "T09:30").replace(tzinfo=NY)
    with pytest.raises(SafetyError):
        HistoricalDevelopmentSession(opened=opened, closed=opened + timedelta(hours=6, minutes=30), bars=ForbiddenRows())


@pytest.mark.parametrize("stage", [None, "validation", "internal_holdout", "external_oos", "oos"])
def test_session_stage_firewall(stage):
    with pytest.raises(SafetyError):
        HistoricalDevelopmentSession(stage=stage, opened=None, closed=None, bars={})


@pytest.mark.parametrize("source", [None, "ALPACA", "HISTORICAL", "", 123])
def test_unknown_provenance(source):
    with pytest.raises(SafetyError):
        require_provenance(source, stage="development")


def test_session_is_immutable_and_no_spoofed_subclass():
    s = generated(H)
    with pytest.raises(FrozenInstanceError):
        s.source = S
    with pytest.raises(TypeError):
        s.bars["DIA"] = {}
    class Impostor(HistoricalDevelopmentSession):
        pass
    with pytest.raises(SafetyError):
        Impostor(opened=s.open, closed=s.close, bars={})


def test_metrics_mixed_sources_rejected(spec):
    with pytest.raises(SafetyError, match="MIXED_PROVENANCE"):
        summary([generated_record(0, S), generated_record(1, H)], spec, H)


@pytest.mark.parametrize("stage", ["validation", "internal_holdout", "external_oos"])
def test_historical_records_stage_rejected(spec, stage):
    with pytest.raises(SafetyError):
        summary([generated_record(0, stage=stage)], spec, H)


def test_metrics_empty_historical_context(spec):
    result = summary([], spec, H)
    assert result["source"] == H and result["stage"] == "development"
    assert result["tail_counts"] == dict(scored_trade_count=0, left_tail_lt_minus_1R=0,
                                        left_tail_lt_minus_2R=0, right_tail_gt_plus_1R=0, right_tail_gt_plus_2R=0)


def test_c4_and_metrics_exact_equivalence(spec):
    values = [-3, -2, -1, 0, 1, 2, 3, 4, 5, 6]
    a = summary([generated_record(r, S) for r in values], spec, S)
    b = summary([generated_record(r, H) for r in values], spec, H)
    assert economic(a) == economic(b)
    assert list(b["tail_counts"].values()) == [10, 2, 1, 5, 4]
    assert b["fixture_kind"] == GENERATED_FIXTURE


def daily():
    return {f"2018-01-{d:02d}": {"R": float(d % 3 - 1), "count": 1} for d in range(4, 11)}


def test_bootstrap_exact_equivalence(spec):
    assert lower_bound(daily(), "development", spec, source=S) == lower_bound(daily(), "development", spec, source=H)


@pytest.mark.parametrize("stage", ["validation", "internal_holdout", "external_oos"])
def test_bootstrap_later_stage_rejected(spec, stage):
    with pytest.raises(SafetyError):
        lower_bound(daily(), stage, spec, source=H)


def test_bootstrap_later_date_rejected(spec):
    with pytest.raises(SafetyError):
        lower_bound({"2022-01-01": {"R": 0, "count": 0}}, "development", spec, source=H)


def test_gate_equivalence_and_truthful_output(spec):
    a = passing_bundle()
    a["fixture_kind"] = GENERATED_FIXTURE
    b = {**a, "source": H}
    sa, hb = evaluate(a, "development", spec), evaluate(b, "development", spec)
    assert sa["source"] == S and hb["source"] == H
    assert economic(sa) == economic(hb)
    assert hb["status"] == "PASS" and hb["fixture_kind"] == GENERATED_FIXTURE


@pytest.mark.parametrize("stage", ["validation", "internal_holdout", "external_oos"])
def test_gate_later_stage_rejected(spec, stage):
    with pytest.raises(SafetyError):
        evaluate({**passing_bundle(), "source": H}, stage, spec)


@pytest.mark.parametrize("scenario", ["BASELINE", "STRESS", "SEVERE"])
@pytest.mark.parametrize("variant", ["CSLC_L15", "CSLC_L30", "CSLC_L45"])
def test_pipeline_exact_equivalence(spec, scenario, variant):
    sessions = [generated(mode) for mode in (S, H)]
    features = [panel(s, spec, variant)[0] for s in sessions]
    assert features[0] is not None
    assert economic(features[0]) == economic(features[1])
    intents = [build_intent(f, s, spec, 5)[0] for f, s in zip(features, sessions)]
    assert intents[0] is not None
    assert economic(intents[0]) == economic(intents[1])
    results = [simulate_session(s, spec, variant, scenario) for s in sessions]
    assert economic(results[0]) == economic(results[1])
    assert results[0]["source"] == S and results[1]["source"] == H


@pytest.mark.parametrize("case", ["ordinary", "halt", "excluded"])
def test_paired_masks_halts_and_scored_fill(spec, case):
    kwargs = {"halts": [halt("13:11", "13:14")]} if case == "halt" else {"excluded": ["QQQ"]} if case == "excluded" else {}
    a, b = generated(S, **kwargs), generated(H, **kwargs)
    assert economic(simulate_session(a, spec)) == economic(simulate_session(b, spec))
    original_intent = intent()
    delta = a.open.date() - original_intent["signal_at"].date()
    i = {**original_intent, "signal_at": original_intent["signal_at"] + delta,
         "due": original_intent["due"] + delta}
    ra, rb = simulate_intent(a, spec, i, "BASELINE"), simulate_intent(b, spec, i, "BASELINE")
    assert ra["status"] == rb["status"] == "SCORED"
    assert economic(ra) == economic(rb)
    assert rb["source"] == H and rb["fixture_kind"] == GENERATED_FIXTURE


def test_d1_synthetic_wrapper_rejects_historical(spec):
    with pytest.raises(SafetyError):
        evaluate_synthetic_sessions([generated(H)], spec)


@pytest.mark.parametrize("command", ["development", "validation", "holdout", "oos", "trade", "paper", "live"])
def test_d1_command_stays_closed(command):
    with pytest.raises(SafetyError):
        require_command(command)


def test_runner_and_research_authority_unchanged():
    root = Path(__file__).resolve().parents[3]
    for path in ["trading_research/v2/runner.py", "trading_research/v2/bindings.py",
                 "trading_research/v2/numeric.py", "trading_research/v2/fills.py", "trading_research/v2/clock.py"]:
        old = subprocess.check_output(["git", "-c", "safe.directory=*", "show", "24496a5ddc307b29cabc4806ce6423c88261bc52:" + path], cwd=root)
        assert old.replace(b"\r\n", b"\n") == (root / path).read_bytes().replace(b"\r\n", b"\n")
    require_command("validate")
    require_command("conformance")


def ledger(tmp_path, spec, mode):
    return Ledger(tmp_path, spec, provenance=mode, fixture_kind=GENERATED_FIXTURE)


def test_historical_ledger_explicit_fixture_exposure(spec, tmp_path):
    log = ledger(tmp_path, spec, H)
    start = log.begin(identity(spec), source=H, historical_price_rows_exposed=0)
    finish = log.finish(start["trial_id"], 1, status="PASS", outcome_hash="d" * 64, historical_price_rows_exposed=7)
    assert finish["source"] == H and finish["historical_price_rows_exposed"] == 7
    assert finish["exposure_accounting_scope"] == GENERATED_FIXTURE
    assert len(log.read()) == 2  # Seven modeled fixture rows, ZERO actual archive rows.


@pytest.mark.parametrize("stage", ["validation", "internal_holdout", "external_oos"])
def test_historical_ledger_later_slots_rejected(spec, tmp_path, stage):
    slot = next(s for s in schedule(spec) if s[0] == stage)
    with pytest.raises(SafetyError):
        ledger(tmp_path, spec, H).begin(identity(spec, slot), source=H, historical_price_rows_exposed=0)
    assert not list(tmp_path.glob("event-*.json"))


@pytest.mark.parametrize("mode,wrong", [(S, H), (H, S)])
def test_ledger_mixed_sources_rejected(spec, tmp_path, mode, wrong):
    with pytest.raises(SafetyError):
        ledger(tmp_path, spec, mode).begin(identity(spec), source=wrong, historical_price_rows_exposed=0)


def test_ledger_cannot_reopen_in_other_mode(spec, tmp_path):
    ledger(tmp_path, spec, H).begin(identity(spec), source=H, historical_price_rows_exposed=0)
    with pytest.raises(SafetyError):
        ledger(tmp_path, spec, S).read()


def test_identity_changes_but_registered_slots_unchanged(spec):
    a = trial_identity(identity(spec), spec, provenance=S)[0]
    b = trial_identity(identity(spec), spec, provenance=H)[0]
    assert a != b and b == trial_identity(identity(spec), spec, provenance=H)[0]
    assert len(schedule(spec)) == 53
    assert sum(s[0] == "development" for s in schedule(spec)) == 26


def test_synthetic_exposure_remains_zero(spec, tmp_path):
    event = ledger(tmp_path, spec, S).begin(identity(spec), source=S)
    assert event["historical_price_rows_exposed"] == 0
    with pytest.raises(SafetyError):
        ledger(tmp_path, spec, S).begin(identity(spec), source=S, historical_price_rows_exposed=1)


@pytest.mark.parametrize("count", [None, -1, True, 1.5])
def test_historical_exposure_must_be_explicit_integer(spec, tmp_path, count):
    with pytest.raises(SafetyError):
        ledger(tmp_path, spec, H).begin(identity(spec), source=H, historical_price_rows_exposed=count)


def test_no_trade_preserves_mode():
    result = no_trade([generated(H)])[0]
    assert result["source"] == H and result["daily_R"] == 0


@pytest.mark.parametrize("options", [{"delay_diagnostic": True}, {"control": True}])
def test_independent_control_paths_equivalent(spec, options):
    a = simulate_session(generated(S), spec, scenario="STRESS", **options)
    b = simulate_session(generated(H), spec, scenario="STRESS", **options)
    assert economic(a) == economic(b)
    assert a["source"] == S and b["source"] == H


def test_fixture_and_actual_records_cannot_mix(spec):
    a = generated_record(0)
    b = {**generated_record(1), "fixture_kind": None}
    with pytest.raises(SafetyError, match="MIXED_FIXTURE"):
        summary([a, b], spec, H)


def test_exposure_cannot_decrease(spec, tmp_path):
    log = ledger(tmp_path, spec, H)
    start = log.begin(identity(spec), source=H, historical_price_rows_exposed=7)
    with pytest.raises(SafetyError, match="CANNOT_DECREASE"):
        log.finish(start["trial_id"], 1, status="PASS", outcome_hash="d" * 64, historical_price_rows_exposed=0)


def test_cross_day_rejected():
    a = generated(H)
    with pytest.raises(SafetyError, match="CROSS_DAY"):
        HistoricalDevelopmentSession(opened=a.open, closed=a.close + timedelta(days=1), bars={})
