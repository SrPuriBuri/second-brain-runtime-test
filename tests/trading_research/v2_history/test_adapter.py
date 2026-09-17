"""GENERATED_ADAPTER_FIXTURE. All prices below are invented constants."""

from copy import deepcopy
from datetime import datetime, timedelta
from dataclasses import FrozenInstanceError
import gzip
import hashlib
import json

import pytest

from trading_research.v2.bindings import canonical, digest
from trading_research.v2.ledger import trial_identity
from trading_research.v2.models import NY
from trading_research.v2.runner import require_command
from trading_research.v2.simulator import simulate_session
from trading_research.v2_protocol import UNIVERSE
from trading_research.v2_history.adapter import boundary, partitions, map_rows, make_session, DevelopmentArchive
from trading_research.v2_history.audit import Access
from trading_research.v2_history.development_runner import execute, trial_value
from trading_runtime.config import SafetyError


def row(day="2018-01-04", time="09:30"):
    return {"t": day+"T"+time+":00-05:00", "o": "100.125000", "h": "100.25", "l": "100.0", "c": "100.125", "v": "100000"}


def cal(day="2018-01-04", close="16:00"):
    return {"date": day, "open": "09:30", "close": close}


def item(day="2018-01-04", symbol="DIA"):
    return dict(first_session=day, last_session=day, session_dates=[day], symbol=symbol)


def mask():
    return dict(universe=list(UNIVERSE), sessions=[], halts=[], excluded_symbols=["XLRE"])


def mapped(rows=None, day="2018-01-04"):
    return map_rows(rows or [row(day)], item(day), {day: cal(day)}, {"historical_price_rows_read": 0})


@pytest.mark.parametrize("day", ["2016-01-04", "2021-12-31"])
def test_allowed_boundaries(day):
    assert boundary(day, day)[0].isoformat() == day
    assert mapped(day=day)[day][0].open_text == "100.125000"


@pytest.mark.parametrize("day", ["2016-01-03", "2022-01-01", "2024-01-02", "2025-01-02"])
def test_forbidden_dates_before_decode(day):
    class Rows:
        def __iter__(self):
            raise AssertionError("No row may be decoded")
    with pytest.raises(SafetyError):
        map_rows(Rows(), item(day), {}, {"historical_price_rows_read": 0})


@pytest.mark.parametrize("defect", ["duplicate", "order", "off_grid", "cross_day", "decimal_not_text", "naive", "outside_session"])
def test_schema_rejection(defect):
    a, b = row(), row(time="09:35")
    if defect == "duplicate":
        b = a.copy()
    elif defect == "order":
        a, b = b, a
    elif defect == "off_grid":
        b["t"] = "2018-01-04T09:36:00-05:00"
    elif defect == "cross_day":
        b["t"] = "2018-01-05T09:35:00-05:00"
    elif defect == "decimal_not_text":
        b["o"] = 100.125
    elif defect == "naive":
        b["t"] = "2018-01-04T09:35:00"
    else:
        b["t"] = "2018-01-04T16:00:00-05:00"
    with pytest.raises(SafetyError):
        mapped([a, b])


def test_exact_text_no_fill_deterministic_immutable():
    rows = [row(), row(time="09:40")]
    before = deepcopy(rows)
    a, b = mapped(rows), mapped(rows)
    assert a == b and rows == before
    assert len(a["2018-01-04"]) == 2  # No fabricated 09:35 bar.
    assert a["2018-01-04"][0].open_text == "100.125000"
    with pytest.raises(FrozenInstanceError):
        a["2018-01-04"][0].open_text = "101"


def test_exact_panel_source_mask_and_early_close(spec):
    m = mask()
    m["sessions"] = [dict(symbol="QQQ", session="2018-01-04", status="INVALID_DATA_GAP")]
    bars = {s: mapped()["2018-01-04"] for s in UNIVERSE}
    s = make_session(cal(close="13:00"), bars, m)
    assert s.source == "HISTORICAL_DEVELOPMENT_D2" and s.stage == "development"
    assert s.close.hour == 13 and s.excluded == frozenset({"QQQ"})
    assert simulate_session(s, spec)["status"] == "NO_SIGNAL"
    assert s == make_session(cal(close="13:00"), bars, m)


@pytest.mark.parametrize("defect", ["XLRE", "missing", "wrong_mask"])
def test_universe(defect):
    bars = {s: mapped()["2018-01-04"] for s in UNIVERSE}
    m = mask()
    if defect == "XLRE":
        bars["XLRE"] = []
    elif defect == "missing":
        del bars["DIA"]
    else:
        m["universe"] = ["SPY"]
    with pytest.raises(SafetyError):
        make_session(cal(), bars, m)


def test_halts_exactly_mapped():
    m = mask()
    m["halts"] = [dict(start="2018-01-04T12:56:17-05:00", end="2018-01-04T13:11:17-05:00", verified=True, source_ref="GENERATED_ADAPTER_FIXTURE")]
    s = make_session(cal(), {s: [] for s in UNIVERSE}, m)
    assert s.halts[0][0].second == 17
    assert (s.halts[0][1] - s.halts[0][0]).total_seconds() == 900


def test_phase_a_access_denies_price_network():
    a = Access("aist-data")
    with pytest.raises(SafetyError):
        a.allow_price("aist-data/bars/example.jsonl.gz")
    with pytest.raises(SafetyError):
        a.hook("open", ("aist-data/bars/example.jsonl.gz", "r", 0))
    with pytest.raises(SafetyError):
        a.hook("socket.connect", ())
    assert a.counters["historical_price_files_opened"] == 0


def test_future_objects_not_selected_and_partial_month_rejected():
    plan = {"selection": dict(chunking="symbol/month", session_scope="REGULAR", feed="sip", timeframe="5Min", adjustment_modes=["raw"])}
    calendar = [cal("2021-12-31"), cal("2022-01-03")]
    sums, checkpoint = {}, {"chunks": {}}
    for symbol in UNIVERSE:
        params = dict(symbols=symbol, start="2021-12-01T00:00:00Z", end="2021-12-31T23:59:59Z", feed="sip", timeframe="5Min", adjustment="raw", asof="-", sort="asc", limit=10000)
        k = digest({"route": "bars", "params": params})
        sums["bars/"+k+".jsonl.gz"] = "a"*64
        checkpoint["chunks"][k] = {}
    items = partitions(plan, sums, checkpoint, calendar, UNIVERSE, "2021-12-31", "2021-12-31")
    assert len(items) == 13 and all(i["last_session"] == "2021-12-31" for i in items)
    with pytest.raises(SafetyError, match="PARTITION_INSUFFICIENT"):
        partitions(plan, sums, checkpoint, [cal("2021-12-30"), *calendar], UNIVERSE, "2021-12-31", "2021-12-31")


@pytest.mark.parametrize("command", ["development", "validation", "holdout", "oos", "trade", "paper", "live"])
def test_d1_runner_unchanged(command):
    with pytest.raises(SafetyError):
        require_command(command)


def runtime(spec):
    return dict(D1R_implementation_hash="b734d99d858689eba22c9ac33f06cae13657c37e4ae723610e78c79914c42a93",
                D2_adapter_hash="a"*64, D2_development_runtime_hash="b"*64,
                execution_config_hash="c"*64, dependency_fingerprint="d"*64)


def test_adapter_runtime_bound_in_frozen_trial_identity(spec):
    from trading_research.v2.ledger import schedule
    slot = schedule(spec)[0]
    a = trial_value(slot, spec, runtime(spec))
    b = trial_value(slot, spec, {**runtime(spec), "D2_adapter_hash": "e"*64})
    assert trial_identity(a, spec, provenance="HISTORICAL_DEVELOPMENT_D2")[0] != trial_identity(b, spec, provenance="HISTORICAL_DEVELOPMENT_D2")[0]
    assert json.loads(a["purpose"])["D2_adapter_hash"] == "a"*64
    with pytest.raises(SafetyError):
        trial_value(("validation", *slot[1:]), spec, runtime(spec))


def generated_archive(tmp_path):
    calendar = [cal("2018-01-"+str(d).zfill(2)) for d in range(4, 9)]
    items = []
    for symbol in UNIVERSE:
        rows = []
        for c in calendar:
            t = datetime.fromisoformat(c["date"]+"T09:30:00").replace(tzinfo=NY)
            for _ in range(78):
                rows.append({**row(c["date"]), "t": t.isoformat()})
                t += timedelta(minutes=5)
        data = gzip.compress(b"".join(canonical(r)+b"\n" for r in rows), mtime=0)
        path = "bars/"+symbol+".jsonl.gz"
        (tmp_path/"bars").mkdir(exist_ok=True)
        (tmp_path/path).write_bytes(data)
        items.append(dict(path=path, sha256=hashlib.sha256(data).hexdigest(), symbol=symbol,
                          first_session="2018-01-04", last_session="2018-01-08", session_dates=[c["date"] for c in calendar]))
    access = Access(tmp_path)
    access.phase_b = True
    return DevelopmentArchive(tmp_path, items, calendar, mask(), access), access


def test_generated_structural_and_complete_26_schedule(spec, tmp_path):
    archive, access = generated_archive(tmp_path)
    bars, audit = archive.structural_audit()
    assert audit["decoded_bar_rows"] == 5070 and audit["signals_calculated"] == 0
    assert not audit["missing_slots_by_symbol"]
    out = tmp_path/"generated-results"
    result = execute(bars, archive, spec, runtime(spec), out, access)
    assert result["completed_records"] == 26
    assert len(list((out/"DEVELOPMENT_TRIAL_LEDGER_V2").glob("event-*.json"))) == 52
    assert result["global_unique_historical_price_rows_exposed_to_strategy"] == 5070
    assert len(list((out/"outcomes").glob("*.json"))) == 26
    assert all(b.source == "HISTORICAL_DEVELOPMENT_D2" for b in [make_session(archive.calendar[d], bars[d], archive.mask) for d in bars])


def test_unknown_row_provenance_rejected():
    with pytest.raises(SafetyError, match="EXACT_TEXT_SCHEMA"):
        mapped([{**row(), "source": "UNKNOWN"}])


def test_exclusion_blocks_feature_calculation(spec):
    m = mask()
    m["sessions"] = [dict(symbol="QQQ", session="2018-01-04", status="INVALID_DATA_GAP")]
    s = make_session(cal(), {name: [] for name in UNIVERSE}, m)
    assert simulate_session(s, spec)["reason"] == "NO_SIGNAL_QC_EXCLUDED"


def test_no_provider_or_network_imports():
    import ast
    from pathlib import Path
    root = Path(__file__).resolve().parents[3] / "trading_research/v2_history"
    forbidden = ("alpaca", "requests", "httpx", "urllib", "socket", "trading_research.providers", "trading_runtime.broker")
    for path in root.glob("*.py"):
        for n in ast.walk(ast.parse(path.read_text())):
            if isinstance(n, ast.Import):
                assert not any(a.name.startswith(forbidden) for a in n.names)
            if isinstance(n, ast.ImportFrom):
                assert not (n.module or "").startswith(forbidden)


def test_phase_b_needs_remotely_bound_manifest(tmp_path):
    from trading_research.v2_history.audit import verify_barrier
    import subprocess
    with pytest.raises((SafetyError, subprocess.CalledProcessError)):
        verify_barrier(tmp_path/"private/projects/ai-stock-trader", tmp_path/"public", "0"*40, {})


def test_historical_runner_rejects_other_stages():
    from trading_research.v2_history.development_runner import main
    for command in ("validation", "holdout", "oos"):
        with pytest.raises(SystemExit):
            main([command, "--project", ".", "--data-root", "."])
