"""Synthetic end-to-end archive, interruption, immutable restore and gates."""

import ast
from copy import deepcopy
from datetime import timedelta
import json
from pathlib import Path
import socket
import subprocess
import sys
from types import SimpleNamespace

import httpx
import pytest

from trading_runtime.config import SafetyError
from trading_runtime.market_calendar import session_from_row
from trading_research.archive import (
    Archive,
    chunks,
    data_root,
    jsonl_bytes,
    make_plan,
    normalize_bar,
    portable_summary,
    require_retention,
)
from trading_research.coverage import audit_archive, reconcile
from trading_research.dataset_manifest import sha256
from trading_research.downloader import Downloader, offline_references
from trading_research.providers.alpaca import AlpacaFoundationProvider
from trading_research.restore import read_jsonl, verify_snapshot


PERMITTED = dict(
    status="PERMITTED",
    provider="alpaca",
    feed="sip",
    private_local_retention=True,
    source_ref="synthetic fixture permission ONLY",
    reviewed_at="2024-01-01T00:00:00Z",
)
CALENDAR = [
    dict(date=day, open="09:30", close="16:00") for day in ("2024-03-08", "2024-03-11")
]


def fixture_rows():
    result = []
    for row in CALENDAR:
        s = session_from_row(row)
        for i in range(78):
            result.append(
                dict(
                    t=(s.open + timedelta(minutes=5 * i)).isoformat(),
                    o=100,
                    h=102,
                    l=99,
                    c=101,
                    v=50,
                    n=3,
                    vw=100.50,
                )
            )
    return result


@pytest.fixture
def archive(tmp_path):
    plan = deepcopy(make_plan())
    plan["selection"].update(
        symbols=["SPY"], archive_start="2024-03-08", archive_end="2024-03-11"
    )
    plan["dataset_id"] = sha256(plan["selection"])
    return Archive(tmp_path, plan)


def provider(archive, handler):
    return AlpacaFoundationProvider(
        SimpleNamespace(key="fixture", secret="fixture"),
        archive.cache,
        httpx.Client(transport=httpx.MockTransport(handler)),
        pause=lambda _: None,
    )


def handler(request):
    assert request.method == "GET"
    if request.url.path.endswith("/calendar"):
        return httpx.Response(200, json=CALENDAR)
    if request.url.path.endswith("/assets"):
        return httpx.Response(
            200,
            json=[dict(symbol="SPY", id="fixture-id", status="active", tradable=True)],
        )
    if request.url.path.endswith("/corporate-actions"):
        return httpx.Response(200, json={"corporate_actions": {}})
    assert request.url.path.endswith("/bars")
    return httpx.Response(200, json={"bars": {"SPY": fixture_rows()}})


def identity():
    return {
        "SPY": dict(
            asset_id="fixture-id",
            etf_verified=True,
            identity_continuity_verified=True,
            source_ref="synthetic issuer evidence",
        )
    }


def frozen(archive):
    Downloader(archive, provider(archive, handler), PERMITTED).run()
    quality = audit_archive(archive, identity())
    assert quality["accepted"] and quality["symbols"]["SPY"]["expected"] == 156
    calendar, actions, _ = offline_references(archive)
    path = archive.freeze(
        {"symbols": ["SPY"], "identity_evidence": identity()},
        calendar,
        actions,
        quality,
        "synthetic-code-commit",
    )
    return path


def test_universe_and_chunk_selection_deterministic_no_returns():
    first, second = make_plan(), make_plan()
    assert first["dataset_id"] == second["dataset_id"]
    assert len(list(chunks(first))) == 1512
    assert list(chunks(first)) == list(chunks(second))
    assert "XLC" not in first["selection"]["symbols"]
    assert "return" not in json.dumps(first["selection"]["symbols"])
    assert "NO_RETURNS" in first["selection"]["universe_selection"]
    assert all(c["params"]["end"] < "2025" for c in chunks(first))


def test_storage_terms_fail_before_network(archive):
    with pytest.raises(SafetyError, match="STORAGE_TERMS_UNRESOLVED"):
        Downloader(archive, object(), {})
    with pytest.raises(SafetyError):
        require_retention({**PERMITTED, "private_local_retention": False})
    assert archive.status()["completed_request_pages"] == 0


def test_root_outside_repos_and_configurable(tmp_path):
    runtime = Path(__file__).resolve().parents[2]
    assert data_root({}) == runtime.parent / "aist-data"
    assert data_root({"AIST_DATA_ROOT": str(tmp_path)}) == tmp_path.resolve()
    for repo in (runtime, runtime.parent / "second-brain"):
        with pytest.raises(SafetyError, match="INSIDE_REPOSITORY"):
            data_root({"AIST_DATA_ROOT": str(repo / "data")})


def test_oos_attempt_blocked_and_counted_before_http(archive):
    p = provider(archive, lambda _: pytest.fail("network must not be attempted"))
    d = Downloader(archive, p, PERMITTED)
    with pytest.raises(SafetyError, match="FORBIDDEN"):
        d.page(
            "bars",
            dict(
                symbols="SPY",
                start="2025-01-01",
                end="2025-01-02",
                feed="sip",
                adjustment="raw",
            ),
        )
    assert p.requests == 0
    assert archive.status()["blocked_oos_requests"] == 1
    assert archive.status()["OOS_price_requests"] == 0
    with pytest.raises(SafetyError, match="OOS_ROW"):
        normalize_bar(dict(t="2024-12-31T23:30:00-05:00", o=1, h=1, l=1, c=1, v=1))


def test_end_of_december_never_requests_january():
    last = list(chunks(make_plan()))[-1]
    assert last["params"]["end"] == "2024-12-31T23:59:59Z"


def test_native_pages_preserved_and_normalized_deterministic(archive):
    d = Downloader(archive, provider(archive, handler), PERMITTED)
    d.run()
    values = [archive.read_object(r) for r in archive.index()["requests"].values()]
    raw = next(v for v in values if v["route"] == "bars")["body"]["bars"]["SPY"][0]
    assert raw["n"] == 3 and raw["vw"] == 100.5
    a = normalize_bar(raw)
    b = normalize_bar({**raw, "o": 100.0, "v": "50.000"})
    assert jsonl_bytes([a]) == jsonl_bytes([b])
    assert a["t"] == "2024-03-08T14:30:00Z"


def test_resume_interrupted_second_page_without_first_redownload(archive):
    bar_calls = []

    def broken(request):
        if not request.url.path.endswith("/bars"):
            return handler(request)
        cursor = request.url.params.get("page_token")
        bar_calls.append(cursor)
        if cursor:
            raise httpx.ConnectError("fixture interruption")
        return httpx.Response(
            200, json={"bars": {"SPY": fixture_rows()[:78]}, "next_page_token": "page2"}
        )

    with pytest.raises(SafetyError, match="TRANSPORT"):
        Downloader(archive, provider(archive, broken), PERMITTED).run()
    assert bar_calls == [None, "page2", "page2", "page2"]
    assert archive.status()["completed_chunks"] == 0
    assert archive.status()["completed_request_pages"] == 4

    def resume(request):
        assert (
            request.url.path.endswith("/bars")
            and request.url.params["page_token"] == "page2"
        )
        return httpx.Response(200, json={"bars": {"SPY": fixture_rows()[78:]}})

    fresh = Archive(archive.root, archive.plan)
    result = Downloader(fresh, provider(fresh, resume), PERMITTED).run()
    assert result["completed_chunks"] == 1
    assert audit_archive(fresh, identity())["accepted"]


def test_completed_chunk_reused_without_provider(archive):
    Downloader(archive, provider(archive, handler), PERMITTED).run()
    result = Downloader(
        Archive(archive.root, archive.plan),
        provider(archive, lambda _: pytest.fail("redownload")),
        PERMITTED,
    ).run()
    assert result["new_completed_chunks"] == 0


@pytest.mark.parametrize(
    "codes,expected_requests,success",
    [([429, 200], 2, True), ([503, 503, 200], 3, True), ([429, 429, 429], 3, False)],
)
def test_rate_limit_bounded_retry(archive, codes, expected_requests, success):
    responses = iter(codes)
    p = provider(archive, lambda _: httpx.Response(next(responses), json={"bars": {}}))
    params = next(chunks(archive.plan))["params"]
    if success:
        Downloader(archive, p, PERMITTED).page("bars", params)
    else:
        with pytest.raises(SafetyError, match="HTTP_429"):
            Downloader(archive, p, PERMITTED).page("bars", params)
    assert (
        p.requests == expected_requests
        and len(archive.index()["receipts"]) == expected_requests
    )


def test_freeze_and_fresh_offline_restore(archive, monkeypatch):
    path = frozen(archive)
    monkeypatch.setattr(socket.socket, "connect", lambda *_: pytest.fail("network"))
    restored = verify_snapshot(path)
    assert (
        restored["result"] == "RESTORE_PASS"
        and restored["provider_calls"] == 0
        and restored["rows"] == 156
    )
    assert read_jsonl(path / "calendar/sessions.jsonl.gz") == CALENDAR
    # A separate interpreter has no acquisition memory; forbid its sockets too.
    code = "import socket; socket.socket.connect=lambda *a: (_ for _ in ()).throw(RuntimeError('NETWORK_FORBIDDEN')); from trading_research.restore import verify_snapshot; import json,sys; print(json.dumps(verify_snapshot(sys.argv[1])))"
    result = subprocess.run(
        [sys.executable, "-c", code, str(path)],
        capture_output=True,
        text=True,
        check=True,
    )
    assert json.loads(result.stdout) == restored
    manifest = json.loads((path / "manifest.json").read_text())
    summary = portable_summary(manifest)
    assert "rows" not in summary and "body" not in summary
    assert summary["aggregate_content_hash"] == restored["aggregate_content_hash"]


def test_frozen_snapshot_immutable_and_revision_separate(archive):
    path = frozen(archive)
    original = (path / "manifest.json").read_bytes()
    calendar, actions, _ = offline_references(archive)
    q = audit_archive(archive, identity())
    assert (
        archive.freeze(
            {"symbols": ["SPY"], "identity_evidence": identity()},
            calendar,
            actions,
            q,
            "synthetic-code-commit",
        )
        == path
    )
    ref = next(iter(archive.index()["chunks"].values()))
    payload = archive.read_object(ref)
    payload["rows"][0]["v"] = "51"
    index = archive.index()
    key = next(iter(index["chunks"]))
    index["chunks"][key] = archive.save_object("market", key, payload)
    archive.checkpoint(index)
    revised = archive.freeze(
        {"symbols": ["SPY"], "identity_evidence": identity()},
        calendar,
        actions,
        audit_archive(archive, identity()),
        "synthetic-code-commit",
    )
    assert revised != path and (path / "manifest.json").read_bytes() == original
    assert (
        verify_snapshot(path)["result"]
        == verify_snapshot(revised)["result"]
        == "RESTORE_PASS"
    )


def test_atomic_partial_and_corrupt_snapshot_rejected(archive, monkeypatch):
    path = frozen(archive)
    extra = path.parent / ".partial-incomplete"
    extra.mkdir()
    with pytest.raises(SafetyError, match="RESTORE_FAIL"):
        verify_snapshot(extra)
    target = next((path / "bars").glob("*.gz"))
    target.write_bytes(b"corrupt")
    with pytest.raises(SafetyError, match="RESTORE_FAIL"):
        verify_snapshot(path)


def test_missing_chunk_and_unverified_identity_prevent_freeze(archive):
    Downloader(archive, provider(archive, handler), PERMITTED).run()
    quality = audit_archive(archive, {})
    assert not quality["accepted"]
    calendar, actions, _ = offline_references(archive)
    with pytest.raises(SafetyError, match="QUALITY"):
        archive.freeze({"symbols": ["SPY"]}, calendar, actions, quality, "x")
    index = archive.index()
    index["chunks"] = {}
    archive.checkpoint(index)
    with pytest.raises(SafetyError, match="CHUNKS"):
        archive.freeze(
            {"symbols": ["SPY"]},
            calendar,
            actions,
            {"accepted": True, "identity_verified": True},
            "x",
        )


def test_material_split_and_identity_reconciliation():
    rows = [
        dict(t="2020-08-28T15:00:00Z", o="400", c="400"),
        dict(t="2020-08-31T15:00:00Z", o="100", c="100"),
    ]
    event = dict(
        symbol="SPY",
        action_type="forward_splits",
        ex_date="2020-08-31",
        source_record_id="split",
        terms={"historical_price_multiplier": "0.25"},
    )
    assert reconcile("SPY", rows, [event])["status"] == "PASS_WITH_LIMITATIONS"
    assert reconcile("SPY", rows, [])["status"] == "UNRESOLVED"
    assert reconcile("SPY", [], [event])["status"] == "UNRESOLVED"
    event.update(action_type="name_changes", terms={})
    assert reconcile("SPY", rows, [event])["status"] == "UNRESOLVED"


def test_data_only_modules_have_no_strategy_or_mutation_imports():
    base = Path(__file__).resolve().parents[2] / "trading_research"
    forbidden = {
        "experiment",
        "simulator",
        "metrics",
        "candidates",
        "features",
        "executor",
        "alpaca_client",
        "runner",
        "cli",
        "reconcile",
    }
    for name in ["archive", "downloader", "coverage", "restore", "dataset_cli"]:
        tree = ast.parse((base / (name + ".py")).read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                assert not set((node.module or "").split(".")) & forbidden
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                assert node.func.attr not in {
                    "post",
                    "put",
                    "patch",
                    "delete",
                    "submit_order",
                    "cancel_order",
                    "close_position",
                    "close_all_positions",
                }


def test_writer_lock_and_release(archive):
    with archive.writer():
        with pytest.raises(SafetyError, match="WRITER_BUSY"):
            with archive.writer():
                pytest.fail("second writer entered")
    with archive.writer():
        pass


def test_manifest_tampering_rejected(archive):
    path = frozen(archive)
    manifest = json.loads((path / "manifest.json").read_text())
    manifest["common_coverage_start"] = "2016-01-01"
    (path / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(SafetyError, match="RESTORE_FAIL"):
        verify_snapshot(path)


def test_acceptance_cannot_be_weakened(archive):
    plan = deepcopy(archive.plan)
    plan["selection"]["acceptance"]["min_symbol_completeness"] = 0.5
    plan["dataset_id"] = sha256(plan["selection"])
    with pytest.raises(SafetyError, match="SPEC_INVALID"):
        Archive(archive.root, plan)


def test_multisymbol_multiyear_restore_with_actions(tmp_path):
    plan = deepcopy(make_plan())
    plan["selection"].update(
        symbols=["QQQ", "SPY"], archive_start="2023-12-29", archive_end="2024-01-02"
    )
    plan["dataset_id"] = sha256(plan["selection"])
    archive = Archive(tmp_path, plan)
    calendar = [
        dict(date=day, open="09:30", close="16:00")
        for day in ("2023-12-29", "2024-01-02")
    ]

    def source(request):
        path = request.url.path
        if path.endswith("/calendar"):
            return httpx.Response(200, json=calendar)
        if path.endswith("/assets"):
            return httpx.Response(
                200,
                json=[
                    dict(symbol=s, id=s, tradable=True, status="active")
                    for s in ("SPY", "QQQ")
                ],
            )
        if path.endswith("/corporate-actions"):
            return httpx.Response(
                200,
                json={
                    "corporate_actions": {
                        "cash_dividends": [
                            dict(
                                symbol="SPY",
                                id="div-fixture",
                                rate=0.1,
                                ex_date="2023-12-29",
                            )
                        ]
                    }
                },
            )
        symbol = request.url.params["symbols"]
        rows = []
        for day in calendar:
            if (
                request.url.params["start"][:10]
                <= day["date"]
                <= request.url.params["end"][:10]
            ):
                start = session_from_row(day).open
                rows += [
                    dict(
                        t=(start + timedelta(minutes=5 * i)).isoformat(),
                        o=100,
                        h=101,
                        l=99,
                        c=100,
                        v=10,
                    )
                    for i in range(78)
                ]
        return httpx.Response(200, json={"bars": {symbol: rows}})

    identities = {
        s: dict(
            asset_id=s,
            etf_verified=True,
            identity_continuity_verified=True,
            source_ref="synthetic",
        )
        for s in ("SPY", "QQQ")
    }
    Downloader(archive, provider(archive, source), PERMITTED).run()
    quality = audit_archive(archive, identities)
    assert quality["accepted"]
    cal, actions, _ = offline_references(archive)
    path = archive.freeze(
        {"symbols": ["QQQ", "SPY"], "identity_evidence": identities},
        cal,
        actions,
        quality,
        "fixture",
    )
    code = "import socket; socket.socket.connect=lambda *a: (_ for _ in ()).throw(RuntimeError('NETWORK_FORBIDDEN')); import json,sys; from trading_research.restore import verify_snapshot; print(json.dumps(verify_snapshot(sys.argv[1])))"
    result = json.loads(
        subprocess.check_output([sys.executable, "-c", code, str(path)], text=True)
    )
    assert (
        result["provider_calls"] == 0
        and result["rows"] == 312
        and result["bar_chunks"] == 4
    )
    assert result["corporate_actions"] == 1 and result["result"] == "RESTORE_PASS"
