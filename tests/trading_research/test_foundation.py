"""Synthetic data only: integrity, isolation, and immutable provenance."""

import ast
from dataclasses import replace
from datetime import timedelta
import gzip
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from trading_runtime.config import SafetyError
from trading_runtime.market_calendar import session_from_row
from trading_research.corporate_actions import normalize_alpaca
from trading_research.data_quality import (
    audit_bars,
    pre_oos_interval,
    split_adjustment_check,
    utc_stamp,
)
from trading_research.dataset_cache import DatasetCache
from trading_research.dataset_manifest import dataset_id
from trading_research.foundation import SAMPLES, attempt
from trading_research.providers.alpaca import AlpacaFoundationProvider, ROUTES
from trading_research.security_master import (
    ListingInterval,
    apply_symbol_change,
    universe_at,
)


@pytest.fixture
def selection():
    return dict(
        source="synthetic",
        feed="test",
        symbols=["SPY", "QQQ"],
        universe_version="1",
        start="2024-03-08",
        end="2024-03-12",
        timeframe="5Min",
        adjustment="raw",
        retrieval_parameters={"limit": 100},
        corporate_actions_version="ca1",
        security_master_version="sm1",
    )


def session(day="2024-03-08", close="16:00"):
    return dict(date=day, open="09:30", close=close)


def bars(row):
    s = session_from_row(row)
    return [
        dict(
            t=(s.open + timedelta(minutes=5 * i)).isoformat(),
            o=100,
            h=102,
            l=99,
            c=101,
            v=20,
        )
        for i in range(int((s.close - s.open).total_seconds() / 300))
    ]


def test_selection_hash_canonical(selection):
    other = dict(reversed(list(selection.items())))
    other["symbols"] = ["qqq", "SPY", "SPY"]
    assert dataset_id(other) == dataset_id(selection)


@pytest.mark.parametrize(
    "field",
    [
        "source",
        "feed",
        "symbols",
        "universe_version",
        "start",
        "end",
        "timeframe",
        "adjustment",
        "retrieval_parameters",
        "corporate_actions_version",
        "security_master_version",
    ],
)
def test_selection_hash_binds_every_dimension(selection, field):
    changed = dict(selection)
    changed[field] = {
        "start": "2024-03-07",
        "end": "2024-03-13",
        "symbols": ["AAPL"],
        "retrieval_parameters": {"limit": 101},
    }.get(field, "different")
    assert dataset_id(changed) != dataset_id(selection)


def test_cache_versions_are_immutable_and_reusable(tmp_path, selection):
    cache = DatasetCache(tmp_path)
    first = cache.freeze("market", selection, [1, 2])
    assert cache.freeze("market", selection, [1, 2]) == first
    revised = cache.freeze("market", selection, [1, 3])
    assert first["dataset_id"] == revised["dataset_id"]
    assert first["snapshot_id"] != revised["snapshot_id"]
    assert cache.read("market", first["dataset_id"], first["snapshot_id"]) == [1, 2]


def test_cache_interrupted_write_is_never_final(tmp_path, selection, monkeypatch):
    cache = DatasetCache(tmp_path)

    def interrupted(*args):
        raise OSError("interrupted")

    monkeypatch.setattr("trading_research.dataset_cache.os.rename", interrupted)
    with pytest.raises(OSError):
        cache.freeze("market", selection, [1])
    assert list(tmp_path.rglob(".partial-*"))
    assert not any(
        p.name == "payload.json.gz" and not p.parent.name.startswith(".partial-")
        for p in tmp_path.rglob("payload.json.gz")
    )
    with pytest.raises(SafetyError, match="INCOMPLETE"):
        cache.read("market", dataset_id(selection), "a" * 64)


def test_cache_rejects_corruption_and_traversal(tmp_path, selection):
    cache = DatasetCache(tmp_path)
    meta = cache.freeze("market", selection, [1])
    target = tmp_path / "market" / meta["dataset_id"] / meta["snapshot_id"]
    (target / "payload.json.gz").write_bytes(gzip.compress(b"[2]"))
    with pytest.raises(SafetyError, match="CORRUPT"):
        cache.read("market", meta["dataset_id"], meta["snapshot_id"])
    with pytest.raises(SafetyError, match="PATH"):
        cache.read("../escape", "a" * 64, "b" * 64)


@pytest.mark.parametrize(
    "day,open_utc,expected",
    [
        ("2024-03-08", "14:30", 78),
        ("2024-03-11", "13:30", 78),
        ("2024-11-01", "13:30", 78),
        ("2024-11-04", "14:30", 78),
        ("2024-11-29", "14:30", 42),
    ],
)
def test_dst_early_close_completeness(day, open_utc, expected):
    row = session(day, "13:00" if expected == 42 else "16:00")
    q = audit_bars({"SPY": bars(row)}, [row], ["SPY"])["symbols"]["SPY"]
    assert (
        q["expected"] == expected and q["missing"] == 0 and q["status"] == "SAMPLE_PASS"
    )
    assert q["sessions"][day]["open_utc"][11:16] == open_utc


def test_duplicates_order_missing_and_zero_volume():
    row = session()
    values = bars(row)
    values[0]["v"] = 0
    values = [values[1], values[0], values[0], *values[3:]]
    q = audit_bars({"SPY": values}, [row], ["SPY"])["symbols"]["SPY"]
    assert q["missing"] == 1
    assert (
        q["counts"]["duplicates"]
        == q["counts"]["out_of_order"]
        == q["counts"]["zero_volume"]
        == 1
    )
    assert "UNATTRIBUTED" in q["missing_cause"]


@pytest.mark.parametrize(
    "field,value",
    [("o", 0), ("c", -1), ("l", 103), ("h", 98), ("v", -2), ("h", float("nan"))],
)
def test_invalid_ohlcv(field, value):
    row = session()
    values = bars(row)
    values[0][field] = value
    q = audit_bars({"SPY": values}, [row], ["SPY"])["symbols"]["SPY"]
    assert q["missing"] == q["counts"]["invalid_ohlcv"] == 1


def test_timezone_normalization_and_naive_rejection():
    assert utc_stamp("2024-03-08T09:30:00-05:00") == utc_stamp("2024-03-08T14:30:00Z")
    with pytest.raises(SafetyError, match="NAIVE"):
        utc_stamp("2024-03-08T09:30:00")


@pytest.mark.parametrize(
    "start,end",
    [
        ("2024-01-01", "2025-01-01"),
        ("2026-01-01", "2026-02-01"),
        ("2024-12-31T23:30:00-05:00", "2024-12-31T23:45:00-05:00"),
    ],
)
def test_oos_boundary_rejected(start, end):
    with pytest.raises(SafetyError, match="FORBIDDEN"):
        pre_oos_interval(start, end)


def test_unexpected_oos_row_rejected():
    with pytest.raises(SafetyError, match="OOS_PRICE"):
        audit_bars({"SPY": [{"t": "2025-01-01T10:00:00Z"}]}, [session()], ["SPY"])
    for _, start, end, _ in SAMPLES:
        pre_oos_interval(start, end)


@pytest.mark.parametrize(
    "kind,old,new,ratio",
    [("forward_splits", 1, 4, "4"), ("reverse_splits", 10, 1, "0.1")],
)
def test_split_normalization(kind, old, new, ratio):
    event = normalize_alpaca(
        kind,
        dict(
            id="fixture",
            symbol="TEST",
            old_rate=old,
            new_rate=new,
            ex_date="2020-08-31",
            process_date="2020-08-30",
        ),
        "2024-01-01T00:00:00Z",
    )
    assert event["terms"]["share_multiplier"] == ratio
    assert event["announcement_date"] is None and event["known_at"] is None
    assert event["effective_date"] is None


def test_invalid_split_and_symbol_change_date_not_invented():
    with pytest.raises(SafetyError, match="SPLIT"):
        normalize_alpaca(
            "forward_splits",
            dict(id="x", symbol="X", old_rate=0, new_rate=1),
            "2024-01-01T00:00:00Z",
        )
    e = normalize_alpaca(
        "name_changes",
        dict(id="x", old_symbol="FB", new_symbol="META", process_date="2022-06-09"),
        "2024-01-01T00:00:00Z",
    )
    assert (
        e["symbol"] == "FB"
        and e["new_symbol"] == "META"
        and e["effective_date"] is None
    )


def test_split_factor_diagnostic_is_event_based():
    raw = {
        "X": [
            dict(t="2020-08-28T14:00:00Z", c=400),
            dict(t="2020-08-31T14:00:00Z", c=100),
        ]
    }
    adjusted = {
        "X": [
            dict(t="2020-08-28T14:00:00Z", c=100),
            dict(t="2020-08-31T14:00:00Z", c=100),
        ]
    }
    events = [
        dict(
            symbol="X",
            ex_date="2020-08-31",
            source_record_id="x",
            terms={"share_multiplier": "4"},
        )
    ]
    check = split_adjustment_check(raw, adjusted, events)
    assert (
        check["checks"][0]["status"] == "MATCH"
        and check["performance_computed"] is False
    )


def listing():
    return ListingInterval(
        "stable1",
        "OLD",
        "2010-01-01",
        None,
        "NYSE",
        "common",
        "2010-01-01T12:00:00Z",
        "vintage1",
    )


def test_listing_interval_and_current_universe_rejected():
    with pytest.raises(SafetyError, match="INTERVAL"):
        replace(listing(), listing_end="2009-01-01")
    with pytest.raises(SafetyError, match="CURRENT_SNAPSHOT"):
        universe_at([listing()], "2020-01-01T15:00:00Z")


def test_pit_symbol_change_preserves_prior_vintage():
    old = listing()
    closed, opened = apply_symbol_change(
        old,
        dict(
            old_symbol="OLD",
            new_symbol="NEW",
            effective_date="2022-06-09",
            known_at="2022-06-08T12:00:00Z",
            source_record_id="notice",
        ),
    )
    records = [old, closed, opened]
    # Closing a ticker alias does not mean the continuing security is delisted.
    assert closed.delisted is False and opened.security_id == old.security_id
    assert universe_at(records, "2020-01-01T15:00:00Z", True) == (old,)
    assert universe_at(records, "2022-06-08T15:00:00Z", True) == (closed,)
    assert universe_at(records, "2022-06-09T15:00:00Z", True) == (opened,)
    with pytest.raises(SafetyError, match="PIT_EVIDENCE"):
        apply_symbol_change(
            old,
            {"old_symbol": "OLD", "new_symbol": "NEW", "process_date": "2022-06-09"},
        )


def test_pit_future_knowledge_and_conflict():
    future = replace(listing(), known_at="2023-01-01T00:00:00Z")
    assert not universe_at([future], "2020-01-01T15:00:00Z", True)
    with pytest.raises(SafetyError, match="CONFLICTING"):
        universe_at(
            [listing(), replace(listing(), listing_end="2021-01-01")],
            "2020-01-01T15:00:00Z",
            True,
        )


def provider(tmp_path, handler):
    return AlpacaFoundationProvider(
        SimpleNamespace(key="fixture-key", secret="fixture-secret"),
        DatasetCache(tmp_path),
        httpx.Client(transport=httpx.MockTransport(handler)),
        pause=lambda _: None,
    )


def test_capabilities_are_honest_and_routes_paper():
    caps = AlpacaFoundationProvider.capabilities
    assert (
        not caps.supports_delisted
        and not caps.supports_point_in_time
        and not caps.supports_news_revisions
    )
    assert caps.evidence_level == "DOCUMENTED_PENDING_ACCOUNT_PROBES"
    assert all("paper-api.alpaca.markets" in ROUTES[x] for x in ("assets", "calendar"))


def test_get_only_pagination_and_memo(tmp_path):
    seen = []

    def handler(request):
        assert request.method == "GET"
        seen.append(str(request.url))
        page = request.url.params.get("page_token")
        return httpx.Response(
            200,
            json={
                "bars": {
                    "SPY": [{"t": "2024-01-01T00:00:00Z", "c": 1 if not page else 2}]
                },
                "next_page_token": None if page else "fixture-page",
            },
        )

    p = provider(tmp_path, handler)
    first = p.bars(["SPY"], "2024-01-01", "2024-01-02", "sip")
    assert len(first["SPY"]) == 2 and len(seen) == 2
    assert (
        p.bars(["SPY"], "2024-01-01", "2024-01-02", "sip") == first and len(seen) == 2
    )
    assert "fixture-key" not in json.dumps(p.snapshots)
    with pytest.raises(SafetyError, match="ROUTE"):
        p.get("orders", {})
    with pytest.raises(SafetyError, match="FORBIDDEN"):
        p.bars(["SPY"], "2025-01-01", "2025-01-02", "sip")


def test_denial_truth_null_bars_and_news_metadata(tmp_path):
    p = provider(
        tmp_path, lambda r: httpx.Response(403, text="never log private response")
    )
    assert attempt(
        lambda: p.corporate_actions(["SPY"], "2020-01-01", "2021-01-01")
    ) == {"status": "FOUNDATION_HTTP_403"}
    p = provider(
        tmp_path,
        lambda r: httpx.Response(
            200,
            json={
                "bars": None,
                "news": [
                    {"id": 1, "content": "copyright full text", "headline": "fixture"}
                ],
            },
        ),
    )
    assert p.bars(["SPY"], "2020-01-01", "2021-01-01", "iex") == {}
    assert "content" not in p.news(["SPY"], "2020-01-01", "2021-01-01")["metadata"][0]


def test_foundation_has_no_engine_or_broker_mutation_dependencies():
    root = Path(__file__).resolve().parents[2]
    paths = list((root / "trading_research/providers").glob("*.py"))
    paths += [
        root / "trading_research" / (name + ".py")
        for name in (
            "foundation",
            "data_quality",
            "dataset_cache",
            "dataset_manifest",
            "security_master",
            "corporate_actions",
        )
    ]
    forbidden = {
        "simulator",
        "simulation",
        "experiment",
        "metrics",
        "candidates",
        "features",
        "executor",
        "alpaca_client",
        "reconcile",
        "runner",
        "cli",
    }
    for path in paths:
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                assert not set((node.module or "").split(".")) & forbidden
            if isinstance(node, ast.Import):
                assert not any(
                    set(alias.name.split(".")) & forbidden for alias in node.names
                )
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                assert node.func.attr not in {
                    "post",
                    "put",
                    "delete",
                    "patch",
                    "submit_order",
                    "cancel_order",
                    "replace_order",
                    "close_all_positions",
                    "close_position",
                }
    workflow = (root / ".github/workflows/ai-stock-trader-data.yml").read_text()
    assert (
        "workflow_dispatch" in workflow
        and "schedule:" not in workflow
        and "cron:" not in workflow
    )
    assert "inputs:" not in workflow and "actions/upload-artifact" not in workflow
