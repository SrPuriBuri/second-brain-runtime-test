import json
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from trading_runtime.cli import main
from trading_runtime.config import Config, SafetyError
from trading_runtime.market_calendar import display_time, get_session
from trading_runtime.market_data import MarketData
from trading_runtime.models import Handoff, RunRecord, TradeProposal, Watchlist
from trading_runtime.notify import notify
from trading_runtime.private_store import ROOT
from trading_runtime.reconcile import assert_flat, reconcile
from trading_runtime.runner import Runner
from trading_runtime.simulation import NOW, FakeData, dry_run
from trading_runtime.slots import due_slots, slot_times
from trading_runtime.strategist import NoAI
from trading_runtime.universe import asset_reason
from trading_runtime.watchlist import build_watchlist


def enter(context, proposal):
    store, github, broker, schedule, session, evidence, executor = context
    assert (
        executor.submit(
            proposal, "entry", "FIRST_SCAN", NOW, session, schedule
        ).decision
        == "PASS"
    )
    return executor


def test_force_flat_only_owned_and_cobre_untouched(enabled):
    context, proposal = enabled
    broker = context[2]
    broker._positions["MSFT"] = 7
    broker._orders["cobre-order"] = {
        "id": "cobre-order",
        "client_order_id": "cobre-order",
        "symbol": "MSFT",
        "side": "buy",
        "qty": "1",
        "filled_qty": "0",
        "status": "new",
    }
    executor = enter(context, proposal)
    assert not executor.force_flat("flat", NOW)
    assert broker.closures == ["AAPL"]
    assert broker._positions["MSFT"] == 7
    assert "cobre-order" not in broker.cancellations
    assert len(broker.cancellations) == 2  # verified bracket children
    assert not assert_flat(reconcile(broker, context[0], NOW))


def test_ambiguous_same_symbol_never_mutated(enabled):
    context, proposal = enabled
    executor = enter(context, proposal)
    context[2]._positions["AAPL"] += 3
    assert executor.force_flat("flat", NOW)
    assert not context[2].cancellations
    assert not context[2].closures


def test_foreign_same_symbol_order_blocks_management(enabled):
    context, proposal = enabled
    executor = enter(context, proposal)
    context[2]._orders["foreign"] = {
        "id": "foreign",
        "client_order_id": "cobre-foreign",
        "symbol": "AAPL",
        "side": "sell",
        "qty": "1",
        "filled_qty": "0",
        "status": "new",
    }
    assert executor.force_flat("flat", NOW)
    assert not context[2].cancellations


def test_account_binding_missing_blocks_entries(enabled):
    context, proposal = enabled
    context[1].seed("state/ownership-ledger.json", {"account_id": None, "orders": {}})
    executor = context[-1]
    result = executor.submit(
        proposal, "entry", "FIRST_SCAN", NOW, context[4], context[3]
    )
    assert "UNRECONCILED_ACCOUNT" in result.reason_codes
    assert not context[2].submissions


def test_missing_kill_switch_fails_closed(enabled):
    context, proposal = enabled
    del context[1].files[ROOT + "state/kill-switch.json"]
    result = context[-1].submit(
        proposal, "entry", "FIRST_SCAN", NOW, context[4], context[3]
    )
    assert result.decision == "REJECT"
    assert not context[2].submissions


def test_failed_cancel_does_not_close_position(enabled, monkeypatch):
    context, proposal = enabled
    executor = enter(context, proposal)
    monkeypatch.setattr(context[2], "cancel_order", lambda _: None)
    assert executor.force_flat("flat", NOW) == ["FORCE_FLAT_UNRESOLVED"]
    assert not context[2].closures


def test_bracket_fill_reduces_project_position_and_records_loss(enabled):
    context, proposal = enabled
    enter(context, proposal)
    broker = context[2]
    order = next(iter(broker._orders.values()))
    order["legs"][0].update(
        {
            "filled_qty": "25",
            "filled_avg_price": "98",
            "filled_at": NOW.isoformat(),
            "status": "filled",
        }
    )
    order["legs"][1]["status"] = "canceled"
    broker._positions["AAPL"] = 0
    snapshot = reconcile(broker, context[0], NOW)
    assert snapshot.realized_loss == Decimal("50")
    assert not snapshot.owned_positions
    assert not snapshot.alerts


def test_early_close_slots(context):
    broker, schedule = context[2], context[3]
    broker.close_time = "13:00"
    session = get_session(broker, date(2026, 11, 27))
    times = slot_times(session, schedule)
    assert times["FORCE_FLAT"].hour == 12
    assert times["FORCE_FLAT"].minute == 15
    assert times["MANAGE"].hour == 11
    assert times["LAST_NEW_TRADE"].hour == 12
    assert "LAST_NEW_TRADE" not in due_slots(times["FORCE_FLAT"], session, schedule)


def test_madrid_time_and_dst_divergence(context):
    broker = context[2]
    march = get_session(broker, date(2026, 3, 16))
    summer = get_session(broker, date(2026, 9, 10))
    assert "T14:30:00" in display_time(march.open)["Europe/Madrid"]
    assert "T15:30:00" in display_time(summer.open)["Europe/Madrid"]


def test_same_slot_once_across_version_changes(context):
    store, github, broker, *_ = context
    runner = Runner(broker, store, FakeData(), NoAI(), lambda _: [])
    first = runner.scheduled(NOW, "FIRST_SCAN")
    assert first["runs"][0]["status"] == "COMPLETE"
    github.seed(
        "STRATEGY.md",
        '```json\n{"status":"RESEARCH_REQUIRED","version":2,"tradable":false}\n```',
    )
    assert runner.scheduled(NOW, "FIRST_SCAN")["status"] == "NO_OP"


def test_noop_success_no_write_no_notify(context):
    store, github, broker, *_ = context
    sent = []
    runner = Runner(broker, store, FakeData(), NoAI(), lambda s: sent.append(s))
    assert runner.scheduled(NOW - timedelta(hours=6))["status"] == "NO_OP"
    assert not github.writes
    assert not sent


def test_notification_failure_cannot_repeat_slot(context):
    store, github, broker, *_ = context
    runner = Runner(
        broker, store, FakeData(), NoAI(), lambda _: ["DISCORD_NOTIFICATION_FAILED"]
    )
    assert runner.scheduled(NOW)["runs"][0]["status"] == "COMPLETE"
    assert runner.scheduled(NOW)["status"] == "NO_OP"
    assert any("notification_failure" in text for text, _ in github.files.values())
    assert not broker.submissions


def test_optional_notifications_swallow_transport_error(capsys):
    def fail(_):
        raise RuntimeError("synthetic-notification-secret")

    env = {
        "TELEGRAM_BOT_TOKEN": "synthetic-notification-secret",
        "TELEGRAM_CHAT_ID": "synthetic-chat",
    }
    failures = notify(
        {"status": "COMPLETE"}, env, httpx.Client(transport=httpx.MockTransport(fail))
    )
    assert failures == ["TELEGRAM_NOTIFICATION_FAILED"]
    assert "synthetic-notification-secret" not in capsys.readouterr().out


def test_sip_fallback_labels_iex():
    feeds = []

    def handler(request):
        feed = request.url.params["feed"]
        feeds.append(feed)
        return httpx.Response(403 if feed == "sip" else 200, json={})

    data = MarketData(
        Config("synthetic", "synthetic"),
        httpx.Client(transport=httpx.MockTransport(handler)),
    )
    _, feed, notes = data.snapshots(["SPY"], "sip")
    assert feeds == ["sip", "iex"]
    assert feed == "iex"
    assert notes == ["SIP_ENTITLEMENT_UNAVAILABLE_FALLBACK_IEX"]


@pytest.mark.parametrize(
    "patch",
    [
        {"exchange": "OTC"},
        {"tradable": False},
        {"class": "crypto"},
        {"name": "Daily 3x Bull ETF"},
        {"name": "Inverse ETF"},
        {"name": "TEST ASSET"},
    ],
)
def test_universe_filters(context, patch):
    asset = {**context[2].assets()[0], **patch}
    assert asset_reason(asset, context[0].read_policy()) is not None


def test_watchlist_bounded_feed_and_metrics(context):
    store, _, broker, *_ = context
    watchlist = build_watchlist(
        broker.assets(),
        FakeData(),
        store.read_policy(),
        store.read_strategy(),
        NOW,
        NOW.date(),
    )
    assert len(watchlist.candidates) == 10
    assert len(watchlist.rejections) == 5
    assert watchlist.data_feed == "iex"
    assert all(
        c["classification"] == "RESEARCH_CANDIDATE" for c in watchlist.candidates
    )


def test_stale_watchlist_not_padded(context):
    store, _, broker, *_ = context
    watchlist = build_watchlist(
        broker.assets(),
        FakeData(NOW - timedelta(hours=1)),
        store.read_policy(),
        store.read_strategy(),
        NOW,
        NOW.date(),
    )
    assert not watchlist.candidates
    assert all(c["reason"] == "STALE_DATA" for c in watchlist.rejections)


def test_schema_matches_private_if_available():
    root = (
        Path(__file__).resolve().parents[3]
        / "second-brain/projects/ai-stock-trader/schemas"
    )
    if not root.exists():
        pytest.skip(
            "Canonical private schemas are deliberately not required by public CI"
        )
    for name, model in [
        ("trade-proposal", TradeProposal),
        ("run-record", RunRecord),
        ("handoff", Handoff),
        ("watchlist", Watchlist),
    ]:
        assert (
            json.loads((root / f"{name}.schema.json").read_text())
            == model.model_json_schema()
        )


def test_offline_dry_run_and_cli():
    assert dry_run()["orders_submitted"] == 0
    assert main(["dry-run", "--fake"]) == 0


def test_malformed_ai_journaled_and_failed_run_has_handoff(context):
    class BadAI:
        def propose(self, *_):
            raise SafetyError("MALFORMED_AI_JSON")

    store, github, broker, *_ = context
    runner = Runner(broker, store, FakeData(), BadAI(), lambda _: [])
    result = runner.scheduled(NOW)
    assert result["runs"][0]["status"] == "FAILED"
    assert any("data/handoffs/" in path for path in github.files)
    assert not broker.submissions


def test_durable_records_validate_against_schemas(context):
    import jsonschema

    store, github, broker, *_ = context
    runner = Runner(broker, store, FakeData(), NoAI(), lambda _: [])
    assert runner.scheduled(NOW)["runs"][0]["status"] == "COMPLETE"
    checked = set()
    for path, (text, _) in github.files.items():
        for section, model in [
            ("watchlists", Watchlist),
            ("handoffs", Handoff),
            ("progress", RunRecord),
        ]:
            if f"/data/{section}/" in path:
                jsonschema.validate(json.loads(text), model.model_json_schema())
                checked.add(section)
    assert checked == {"watchlists", "handoffs", "progress"}


def test_strategist_receives_bounded_account_evidence_and_exact_handoff(context):
    received = []

    class CaptureAI:
        def propose(self, evidence, strategy_text):
            received.append(evidence)
            return []

    store, github, broker, *_ = context
    runner = Runner(broker, store, FakeData(), CaptureAI(), lambda _: [])
    runner.scheduled(NOW)
    assert received[0]["account_evidence"]["shared_account"] is True
    assert "id" not in received[0]["account_evidence"]
    assert "secret" not in json.dumps(received)
    handoff = store.read_file(store.read_current_state()["handoff"]).json()
    assert "LAST_NEW_TRADE" in handoff["next_action"]


def test_force_flat_persists_post_exit_broker_evidence(enabled):
    context, proposal = enabled
    executor = enter(context, proposal)
    assert not executor.force_flat("flat-evidence", NOW)
    events = [
        json.loads(text)
        for path, (text, _) in context[1].files.items()
        if "/data/journal/flat-evidence/" in path
    ]
    result = next(e for e in events if e["kind"] == "force_flat_result")
    assert not result["data"]["snapshot"]["owned_positions"]
    assert not result["data"]["snapshot"]["owned_orders"]
    assert any(
        o["side"] == "sell" for o in result["data"]["snapshot"]["orders"].values()
    )
