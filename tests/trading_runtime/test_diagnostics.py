import json
from datetime import timedelta

import httpx
import pytest

from trading_runtime.alpaca_client import PaperAlpaca
from trading_runtime.config import Config, SafetyError
from trading_runtime.diagnostics import (
    SAMPLES,
    asset_inventory,
    clean_document,
    isolation_assessment,
    probe,
    run_diagnostics,
)
from trading_runtime.market_data import MarketData
from trading_runtime.simulation import NOW, FakeAlpaca, simulation_store


class DiagnosticBroker(FakeAlpaca):
    def inspect_account(self):
        return self.account()

    def calendar_range(self, start, end):
        days = [start + timedelta(days=i) for i in range(14)]
        result = [
            {"date": str(day), "open": "09:30", "close": "16:00"}
            for day in days
            if day.weekday() < 5
        ]
        result.append({"date": "2026-11-27", "open": "09:30", "close": "13:00"})
        return result


@pytest.fixture
def setup_diagnostic():
    store, github = simulation_store()
    github.seed("state/readiness.json", {"phase": 1, "execution_enabled": False})
    broker = DiagnosticBroker()
    requests = []

    def handler(request):
        assert request.method == "GET"
        requests.append(request)
        path, params = request.url.path, request.url.params
        if params.get("feed") == "sip":
            return httpx.Response(
                403, json={"message": "untrusted content never persisted"}
            )
        if path.endswith("quotes/latest"):
            return httpx.Response(
                200,
                json={
                    "quotes": {
                        s: {"bp": 99, "ap": 100, "t": NOW.isoformat()} for s in SAMPLES
                    }
                },
            )
        if path.endswith("snapshots"):
            return httpx.Response(
                200,
                json={
                    s: {"latestQuote": {"bp": 99, "ap": 100, "t": NOW.isoformat()}}
                    for s in SAMPLES
                },
            )
        if path.endswith("news"):
            return httpx.Response(200, json={"news": []})
        if path.endswith("most-actives"):
            return httpx.Response(403, json={"message": "untrusted"})
        return httpx.Response(200, json={"gainers": [], "losers": []})

    data = MarketData(
        Config("synthetic-diagnostic-key", "synthetic-diagnostic-secret"),
        httpx.Client(transport=httpx.MockTransport(handler)),
    )
    return broker, data, store, github, requests


def test_full_diagnostic_readonly_and_private_persistence(setup_diagnostic):
    broker, data, store, github, requests = setup_diagnostic
    before_strategy = store.required("STRATEGY.md")
    before_kill = store.read_kill_switch()
    report = run_diagnostics(broker, data, store, NOW, {})
    readiness = store.required("state/readiness.json").json()
    assert readiness["paper_verified"] and readiness["iex_working"]
    assert not readiness["sip_entitled"]
    assert readiness["news_working"] and readiness["asset_inventory_complete"]
    assert not readiness["execution_ready"]
    assert not readiness["strategy_tradable"] and readiness["kill_switch_enabled"]
    assert not broker.submissions and not broker.closures and not broker.cancellations
    assert store.required("STRATEGY.md") == before_strategy
    assert store.read_kill_switch() == before_kill
    assert store.read_file(readiness["evidence_ref"]).json() == report
    assert all("[skip ci]" in payload["message"] for _, payload in github.writes)
    assert report["calendar"]["early_close_test"] == "PASS"
    assert report["calendar"]["early_close_example"]["force_flat"][
        "America/New_York"
    ].startswith("2026-11-27T12:15:")
    assert len(report["calendar"]["next_market_days"]) == 7
    assert set(report["commands"]) == {
        "connectivity",
        "inventory",
        "slot-status",
        "reconcile",
        "dry-run",
    }
    assert not any("synthetic-diagnostic" in text for text, _ in github.files.values())
    assert "SIMULATED_ACCOUNT" not in json.dumps(report)
    assert all(request.method == "GET" for request in requests)


def test_cobre_positions_and_orders_untouched_and_unowned(setup_diagnostic):
    broker, data, store, _, _ = setup_diagnostic
    broker._positions["MSFT"] = 5
    broker._orders["foreign"] = {
        "id": "foreign",
        "client_order_id": "cobre-sample",
        "symbol": "MSFT",
        "side": "buy",
        "qty": "1",
        "filled_qty": "0",
        "status": "new",
    }
    report = run_diagnostics(broker, data, store, NOW, {})
    isolation = report["isolation"]
    assert isolation["assessment"] == "UNSAFE"
    assert isolation["shared_with_cobre_alpha"] == "yes"
    assert isolation["unowned_position_count"] == 1
    assert isolation["unowned_order_count"] == 1
    assert broker._positions["MSFT"] == 5
    assert not broker.closures and not broker.cancellations
    assert "cobre-sample" not in json.dumps(report)


def test_empty_unbound_account_is_not_claimed_safe(setup_diagnostic):
    broker, _, store, github, _ = setup_diagnostic
    github.seed("state/ownership-ledger.json", {"account_id": None, "orders": {}})
    isolation = isolation_assessment(broker, store, NOW)
    assert isolation["assessment"] == "SAFE_WITH_LIMITATIONS"
    assert isolation["shared_with_cobre_alpha"] == "unknown"
    assert isolation["recommendation"] == "SEPARATE_PAPER_ACCOUNT"


def test_inventory_actual_flags_exchanges_and_missing_sample():
    assets = [
        {
            "symbol": "SPY",
            "class": "us_equity",
            "status": "active",
            "exchange": "ARCA",
            "tradable": True,
            "fractionable": True,
            "marginable": True,
            "shortable": True,
        },
        {
            "symbol": "OTCT",
            "class": "us_equity",
            "status": "active",
            "exchange": "OTC",
            "tradable": True,
        },
        {
            "symbol": "ZZZ",
            "class": "us_equity",
            "status": "active",
            "exchange": "NASDAQ",
            "tradable": False,
        },
    ]
    result = asset_inventory(assets)
    assert result["active_assets"] == 3 and result["tradable_assets"] == 2
    assert result["otc_assets_excluded"] == 1 and result["tradable_non_otc_assets"] == 1
    assert (
        result["fractionable_assets"]
        == result["marginable_assets"]
        == result["shortable_assets"]
        == 1
    )
    assert not result["samples"]["TSLA"]["active"]


def test_closed_market_old_quotes_can_work_but_not_be_fresh(setup_diagnostic):
    broker, data, store, _, _ = setup_diagnostic
    now = NOW + timedelta(days=3)
    broker.now = now
    broker.is_open = False
    report = run_diagnostics(broker, data, store, now, {})
    assert report["readiness"]["iex_working"]
    assert not report["data_probes"]["iex_quotes"]["samples"]["SPY"]["fresh_for_entry"]
    assert not report["calendar"]["today_is_session"]
    assert report["slot_status"]["due"] == []


@pytest.mark.parametrize(
    "status,expected",
    [
        (401, "AUTHENTICATION_FAILED"),
        (403, "FORBIDDEN_OR_NOT_ENTITLED"),
        (429, "RATE_LIMITED"),
        (500, "HTTP_ERROR"),
    ],
)
def test_probe_status_without_leaking_body(status, expected):
    class Source:
        def get(self, *args):
            return httpx.Response(status, json={"message": "synthetic-do-not-log"})

    result, value = probe(Source(), "/ignored", {}, "quotes")
    assert result["status"] == expected and value is None
    assert "synthetic-do-not-log" not in json.dumps(result)


def test_probe_success_with_wrong_shape_rejected():
    class Source:
        def get(self, *args):
            return httpx.Response(200, json=[])

    assert not probe(Source(), "/ignored", {}, None)[0]["working"]


def test_secret_aliases_redacted():
    env = {
        "Alpaca_API_KEY": "synthetic-key-to-redact",
        "Alpaca_Secret_KEY": "synthetic-secret-to-redact",
    }
    text = json.dumps(clean_document({"notes": list(env.values())}, env))
    assert all(value not in text for value in env.values())


def test_inactive_account_inspectable_but_not_tradable(monkeypatch):
    monkeypatch.setattr(
        "alpaca.trading.client.TradingClient.get_account",
        lambda _: {"id": "fixture", "status": "APPROVAL_PENDING"},
    )
    broker = PaperAlpaca(Config("synthetic", "synthetic"))
    assert broker.inspect_account()["status"] == "APPROVAL_PENDING"
    with pytest.raises(SafetyError, match="PAPER_ACCOUNT_NOT_ACTIVE"):
        broker.account()


def test_diagnostic_refuses_changed_safety_authority(setup_diagnostic):
    broker, data, store, github, _ = setup_diagnostic
    github.seed(
        "state/kill-switch.json",
        {
            "enabled": False,
            "reason": "fixture",
            "updated_at": None,
            "updated_by": "test",
        },
    )
    with pytest.raises(SafetyError, match="PHASE2_SAFETY_STATE_INVALID"):
        run_diagnostics(broker, data, store, NOW, {})
    assert not broker.submissions


def test_workflow_maps_user_secret_names_without_changing_runtime_env():
    from pathlib import Path

    text = (
        Path(__file__).resolve().parents[2]
        / ".github/workflows/ai-stock-trader-paper.yml"
    ).read_text()
    assert "ALPACA_PAPER_API_KEY: ${{ secrets.Alpaca_API_KEY" in text
    assert "ALPACA_PAPER_SECRET_KEY: ${{ secrets.Alpaca_Secret_KEY" in text
    assert "secrets.SECOND_BRAIN_PRIVATE_REPO_TOKEN" in text
    assert "validate-paper" in text
    assert "AI_STOCK_TRADER_RESEARCH_SCHEDULE_ENABLED" in text
    assert "trading_safe_run.py" in text


def test_guarded_output_blocks_raw_secret(capsys):
    from scripts.trading_safe_run import guarded_run

    def leak(_):
        print("unexpected: synthetic-raw-secret")
        return 0

    assert guarded_run([], {"Alpaca_Secret_KEY": "synthetic-raw-secret"}, leak) == 1
    output = capsys.readouterr().out
    assert "synthetic-raw-secret" not in output
    assert "OUTPUT_SECRET_LEAK_PREVENTED" in output


def test_guarded_output_passes_sanitized_result(capsys):
    from scripts.trading_safe_run import guarded_run

    def safe(_):
        print('{"status":"PASS"}')
        return 0

    assert guarded_run([], {"Alpaca_API_KEY": "synthetic-unused-key"}, safe) == 0
    assert json.loads(capsys.readouterr().out) == {"status": "PASS"}
