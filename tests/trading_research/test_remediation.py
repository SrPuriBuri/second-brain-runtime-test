"""B2 metadata masks cannot fill bars, modify parents or cross OOS."""

import ast
from copy import deepcopy
from datetime import date, timedelta
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from trading_research.archive import atomic_json
from trading_research.dataset_manifest import sha256
from trading_research.remediation import (
    build_mask,
    freeze_view,
    gap_inventory,
    halted_slot,
    normalize_in_kind,
    validate_query,
    validity,
    verify_view,
)
from trading_runtime.config import SafetyError

HALT = {
    "id": "official-fixture",
    "start": "2020-03-09T13:34:13Z",
    "end": "2020-03-09T13:49:13Z",
    "verified": True,
    "source_ref": "official synthetic fixture",
}


def test_halt_full_slot_only_without_synthetic_bars():
    assert halted_slot("2020-03-09T13:35:00Z", [HALT])
    assert not halted_slot("2020-03-09T13:30:00Z", [HALT])
    assert not halted_slot("2020-03-09T13:45:00Z", [HALT])
    with pytest.raises(SafetyError):
        halted_slot("2020-03-09T13:35:00Z", [{**HALT, "verified": False}])


def test_gap_correlation_does_not_infer_cause():
    cal = [{"date": "2020-03-09", "open": "09:30", "close": "16:00"}]
    gap = {
        "missing": 1,
        "gaps": [{"start": "2020-03-09T13:35:00Z", "end": "2020-03-09T13:35:00Z"}],
    }
    inv = gap_inventory({"symbols": {"SPY": gap, "QQQ": gap}}, cal)
    assert inv["correlation"][0]["count"] == 2
    assert "classification" not in inv["correlation"][0]


def test_mask_excludes_sessions_and_structural_symbol_without_fill():
    cal = [
        {
            "date": str(date(2020, 1, 1) + timedelta(days=i)),
            "open": "09:30",
            "close": "16:00",
        }
        for i in range(300)
    ]
    inv = {
        "missing": {
            "QQQ": ["2020-01-02T14:30:00+00:00"],
            "XLRE": ["2020-01-02T14:30:00+00:00"],
        }
    }
    ids = {
        "QQQ": {"status": "VERIFIED_WITH_LIMITATIONS"},
        "XLRE": {"status": "UNRESOLVED"},
    }
    original = deepcopy(inv)
    mask = build_mask(inv, cal, [], ids, [])
    assert mask["universe"] == ["QQQ"] and mask["excluded_symbols"] == ["XLRE"]
    assert validity(mask, "QQQ", "2020-01-02T15:00:00Z", cal) == "INVALID_DATA_GAP"
    assert (
        validity(mask, "XLRE", "2020-01-03T15:00:00Z", cal) == "INVALID_EXCLUDED_SYMBOL"
    )
    assert mask["fills"] == 0 and mask["interpolation"] is False and inv == original
    assert sha256(mask) == sha256(build_mask(inv, cal, [], ids, []))


def test_in_kind_action_and_cross_day_mask():
    action = normalize_in_kind(
        {
            "symbol": "XLF",
            "distributed_symbol": "XLRE",
            "ex_date": "2016-09-19",
            "ratio": "0.139146",
            "source_ref": "issuer fixture",
        }
    )
    assert (
        action["action_type"] == "ETF_IN_KIND_DISTRIBUTION"
        and not action["is_cash_dividend"]
    )
    assert not action["knowledge_time_verified"]


@pytest.mark.parametrize(
    "change",
    [
        {"end": "2025-01-01T00:00:00Z"},
        {"end": "2024-01-05T00:00:00Z"},
        {"symbols": "SPY,QQQ"},
    ],
)
def test_target_bounds_and_oos(change):
    q = {
        "route": "trades",
        "purpose": "fixture",
        "params": {
            "start": "2024-01-02T14:30:00Z",
            "end": "2024-01-02T14:31:00Z",
            "symbols": "SPY",
            "feed": "sip",
            "limit": 10000,
        },
    }
    q["params"].update(change)
    with pytest.raises(SafetyError):
        validate_query(q)


def test_child_references_parent_without_mutation(tmp_path):
    parent = tmp_path / "parent"
    parent.mkdir()
    atomic_json(
        parent / "manifest.json",
        {"dataset_id": "fixture-dataset", "snapshot_id": "fixture-snapshot"},
    )
    atomic_json(parent / "checksums.json", {"bars": "retained"})
    before = {p.name: p.read_bytes() for p in parent.iterdir()}
    view = freeze_view(
        tmp_path, parent, "protocol", {"excluded_symbols": ["XLRE"]}, [], {}
    )
    assert {p.name: p.read_bytes() for p in parent.iterdir()} == before
    payload = json.loads(
        (tmp_path / "ai-stock-trader/research-views" / view / "view.json").read_text()
    )
    assert (
        payload["parent_snapshot_id"] == "fixture-snapshot"
        and not payload["raw_bars_duplicated"]
    )
    assert (
        freeze_view(
            tmp_path, parent, "protocol", {"excluded_symbols": ["XLRE"]}, [], {}
        )
        == view
    )


def test_no_strategy_or_mutation_imports():
    path = Path(__file__).resolve().parents[2] / "trading_research/remediation.py"
    tree = ast.parse(path.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            assert not set((node.module or "").split(".")) & {
                "metrics",
                "simulator",
                "candidates",
                "executor",
                "alpaca_client",
            }
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            assert node.func.attr not in {
                "submit_order",
                "cancel_order",
                "close_position",
                "post",
                "put",
                "delete",
                "interpolate",
                "ffill",
            }


def test_child_detects_parent_or_metadata_tampering(tmp_path):
    parent = tmp_path / "parent"
    parent.mkdir()
    atomic_json(parent / "manifest.json", {"dataset_id": "d", "snapshot_id": "s"})
    atomic_json(parent / "checksums.json", {})
    view_id = freeze_view(tmp_path, parent, "p", {}, [], {})
    directory = tmp_path / "ai-stock-trader/research-views" / view_id
    assert verify_view(directory, parent)["result"] == "VIEW_VERIFY_PASS"
    atomic_json(parent / "checksums.json", {"changed": True})
    with pytest.raises(SafetyError):
        verify_view(directory, parent)
    atomic_json(parent / "checksums.json", {})
    atomic_json(directory / "view.json", {"tampered": True})
    with pytest.raises(SafetyError):
        verify_view(directory, parent)


def test_cross_day_action_and_off_grid_cannot_be_valid():
    calendar = [
        {"date": d, "open": "09:30", "close": "16:00"}
        for d in ["2016-09-16", "2016-09-19"]
    ]
    mask = {
        "universe": ["XLF"],
        "sessions": [],
        "halts": [],
        "cross_day_action_boundaries": [{"symbol": "XLF", "date": "2016-09-19"}],
    }
    assert (
        validity(mask, "XLF", "2016-09-19T14:00:00Z", calendar, "2016-09-16T14:00:00Z")
        == "INVALID_CROSS_DAY_ACTION_ADJUSTMENT"
    )
    assert validity(mask, "XLF", "2016-09-19T14:00:01Z", calendar) == "INVALID_OFF_GRID"
    assert validity(mask, "XLF", "2016-09-19T14:00:00Z", calendar) == "VALID"


def test_target_runner_persists_only_bounded_evidence_and_reuses(tmp_path):
    from scripts.trading_remediation_local_run import run

    query = {
        "route": "trades",
        "purpose": "fixture",
        "params": {
            "start": "2024-01-02T14:30:00Z",
            "end": "2024-01-02T14:31:00Z",
            "symbols": "SPY",
            "feed": "sip",
            "limit": 10000,
        },
    }
    calls = []

    def handler(request):
        calls.append(request.method)
        return httpx.Response(
            200,
            json={"trades": {"SPY": [{"t": "2024-01-02T14:30:15Z", "p": 100, "s": 1}]}},
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        directory, result = run(
            {"queries": [query]},
            tmp_path,
            SimpleNamespace(key="fake-key", secret="fake-secret"),
            client,
        )
        run(
            {"queries": [query]},
            tmp_path,
            SimpleNamespace(key="fake-key", secret="fake-secret"),
            client,
        )
    assert calls == ["GET"] and result["outcomes"][0]["rows"] == 1
    for path in directory.rglob("*.json"):
        assert (
            "fake-key" not in path.read_text() and "fake-secret" not in path.read_text()
        )
