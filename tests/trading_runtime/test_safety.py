import json
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

import httpx
import pytest
from pydantic import ValidationError

from trading_runtime.alpaca_client import PaperAlpaca
from trading_runtime.config import (
    Config,
    PAPER_URL,
    SafetyError,
    redact,
    verify_paper_url,
)
from trading_runtime.executor import client_order_id
from trading_runtime.models import TradeProposal
from trading_runtime.policy import evaluate
from trading_runtime.private_store import ROOT, Conflict, PrivateRepoStore
from trading_runtime.reconcile import reconcile
from trading_runtime.risk import size_position
from trading_runtime.simulation import NOW, sample_proposal
from trading_runtime.strategist import parse_proposals


@pytest.mark.parametrize(
    "url",
    [
        "https://api.alpaca.markets",
        PAPER_URL + ".evil.test",
        PAPER_URL + "/",
        "http://paper-api.alpaca.markets",
        "https://paper-api.alpaca.markets@evil.test",
    ],
)
def test_live_endpoint_rejected(url):
    with pytest.raises(SafetyError, match="NOT_PAPER"):
        verify_paper_url(url)


def test_paper_sdk_enum_allowed():
    from alpaca.common.enums import BaseURL

    verify_paper_url(BaseURL.TRADING_PAPER)


def test_missing_credentials_safe():
    with pytest.raises(SafetyError, match="MISSING_PAPER_CREDENTIALS"):
        Config.from_env({})


@pytest.mark.parametrize(
    "key,value",
    [
        ("LIVE", "true"),
        ("PAPER", "false"),
        ("ALPACA_ENDPOINT", PAPER_URL),
        ("ALPACA_LIVE_API_KEY", "synthetic"),
    ],
)
def test_forbidden_config(key, value):
    with pytest.raises(SafetyError):
        Config.from_env(
            {
                "ALPACA_PAPER_API_KEY": "synthetic",
                "ALPACA_PAPER_SECRET_KEY": "synthetic",
                key: value,
            }
        )


def test_secrets_not_in_repr_or_errors(monkeypatch):
    env = {
        "ALPACA_PAPER_API_KEY": "synthetic-key-value",
        "ALPACA_PAPER_SECRET_KEY": "synthetic-secret-value",
        "SECOND_BRAIN_PAT": "synthetic-pat-value",
    }
    config = Config.from_env(env)
    assert all(value not in repr(config) for value in env.values())
    assert all(value not in redact(json.dumps(env), env) for value in env.values())

    def fail(*_):
        raise RuntimeError(env["ALPACA_PAPER_SECRET_KEY"])

    monkeypatch.setattr("alpaca.trading.client.TradingClient.get_account", fail)
    with pytest.raises(SafetyError) as result:
        PaperAlpaca(config)
    assert all(value not in str(result.value) for value in env.values())


def test_production_mutations_hard_disabled(monkeypatch):
    monkeypatch.setattr(
        "alpaca.trading.client.TradingClient.get_account",
        lambda _: {"id": "fixture", "status": "ACTIVE"},
    )
    broker = PaperAlpaca(Config("synthetic", "synthetic"))
    for method, args in [
        (broker.submit_bracket, {}),
        (broker.cancel_order, {"order_id": "fixture"}),
        (
            broker.close_owned,
            {"symbol": "AAPL", "quantity": 1, "client_order_id": "aist-fixture"},
        ),
    ]:
        with pytest.raises(SafetyError, match="PHASE1_EXECUTION_DISABLED"):
            method(**args)


def test_long_structurally_valid():
    assert TradeProposal.model_validate(sample_proposal()).side == "BUY"


@pytest.mark.parametrize(
    "patch",
    [
        {"side": "SELL"},
        {"side": "SHORT"},
        {"stop_price": float("nan")},
        {"confidence": float("inf")},
        {"quantity": 999999},
        {"generated_at": "2026-09-10T14:30:00"},
    ],
)
def test_structural_rejections(patch):
    with pytest.raises(ValidationError):
        TradeProposal.model_validate({**sample_proposal(), **patch})


def submit(context, proposal, run="fixture-run"):
    _, _, _, schedule, session, _, executor = context
    return executor.submit(proposal, run, "FIRST_SCAN", NOW, session, schedule)


def test_kill_and_strategy_block(context):
    result = submit(context, sample_proposal())
    assert {"KILL_SWITCH", "STRATEGY_NOT_TRADABLE"} <= set(result.reason_codes)
    assert not context[2].submissions


@pytest.mark.parametrize(
    "patch,reason",
    [
        ({"stop_price": 100}, "INVALID_STOP"),
        ({"target_price": 99}, "INVALID_TARGET"),
        ({"target_price": 102}, "INSUFFICIENT_REWARD_RISK"),
        ({"data_timestamp": "2026-09-10T12:00:00Z"}, "STALE_DATA"),
        ({"expires_at": NOW.isoformat()}, "EXPIRED_PROPOSAL"),
        ({"max_entry_price": 99}, "INVALID_ENTRY"),
    ],
)
def test_policy_rejections(enabled, patch, reason):
    context, proposal = enabled
    result = submit(context, {**proposal, **patch})
    assert reason in result.reason_codes
    assert not context[2].submissions


def test_market_closed_blocks(enabled):
    context, proposal = enabled
    context[2].is_open = False
    assert "MARKET_CLOSED" in submit(context, proposal).reason_codes


def test_broker_freshness_not_ai_claim(enabled):
    context, proposal = enabled
    context[5]["timestamp"] = "2026-09-10T00:00:00Z"
    assert "STALE_DATA" in submit(context, proposal).reason_codes


def test_risk_size_exact(context):
    size = size_position(
        TradeProposal.model_validate(sample_proposal()),
        context[0].read_policy(),
        20000,
        10000,
    )
    assert size.quantity == 25
    assert size.risk == Decimal("50")
    assert size.notional == Decimal("2500")
    assert size.reward_risk == 2


def test_cash_caps_even_with_margin_buying_power(context):
    size = size_position(
        TradeProposal.model_validate(sample_proposal()),
        context[0].read_policy(),
        100000,
        700,
    )
    assert size.quantity == 7


@pytest.mark.parametrize(
    "field,value,reason",
    [
        ("position_count", 2, "POSITION_LIMIT"),
        ("new_positions", 3, "DAILY_TRADE_LIMIT"),
        ("realized_loss", Decimal("101"), "DAILY_LOSS_LIMIT"),
        ("open_risk", Decimal("101"), "DAILY_LOSS_LIMIT"),
    ],
)
def test_account_limits(enabled, field, value, reason):
    context, proposal = enabled
    store, _, broker, schedule, session, evidence, _ = context
    snapshot = replace(reconcile(broker, store, NOW), **{field: value})
    result = evaluate(
        TradeProposal.model_validate(proposal),
        store.read_policy(),
        store.read_strategy(),
        store.read_kill_switch(),
        snapshot,
        session,
        schedule,
        "FIRST_SCAN",
        NOW,
        broker.clock(),
        evidence,
        True,
        strategy_qualified=True,
    )
    assert reason in result.reason_codes


def test_duplicate_blocked(enabled):
    context, proposal = enabled
    assert submit(context, proposal).decision == "PASS"
    assert "DUPLICATE_ORDER" in submit(context, proposal, "second-run").reason_codes
    assert len(context[2].submissions) == 1


def test_client_id_deterministic():
    proposal = TradeProposal.model_validate(sample_proposal())
    assert client_order_id(proposal) == client_order_id(proposal)
    assert client_order_id(proposal).startswith("aist-")
    assert len(client_order_id(proposal)) <= 48
    assert client_order_id(proposal) != client_order_id(
        proposal.model_copy(update={"proposal_id": "another"})
    )


def test_crash_after_submit_recovered_without_repeat(enabled):
    context, proposal = enabled
    store, _, broker, *_ = context
    broker.crash_after_submit = True
    with pytest.raises(SafetyError, match="SUBMISSION_UNCERTAIN"):
        submit(context, proposal)
    snapshot = reconcile(broker, store, NOW)
    assert snapshot.owned_positions == {"AAPL": Decimal("25")}
    assert not snapshot.alerts
    assert "DUPLICATE_ORDER" in submit(context, proposal, "recovery").reason_codes
    assert len(broker.submissions) == 1


def test_malformed_ai_json_never_reaches_broker(context):
    with pytest.raises(SafetyError, match="MALFORMED_AI_JSON"):
        parse_proposals('{"invoke":"submit_order"}')
    assert submit(context, {"symbol": "AAPL"}).reason_codes == ("MALFORMED_PROPOSAL",)
    assert not context[2].submissions


@pytest.mark.parametrize(
    "path",
    [
        "README.md",
        "projects/other/x.json",
        ROOT + "../outside.json",
        ROOT + "data/evidence/../../STRATEGY.md",
        ROOT + "data/evidence/%2e%2e/x.json",
        ROOT + "data\\evidence\\x.json",
        ROOT + "data//evidence/x.json",
    ],
)
def test_private_path_restrictions(context, path):
    with pytest.raises(SafetyError):
        context[0].create_append_only_record(path, {})


@pytest.mark.parametrize(
    "path",
    ["STRATEGY.md", "POLICY.md", "routines/schedule.json", "state/kill-switch.json"],
)
def test_runtime_cannot_mutate_authority(context, path):
    with pytest.raises(SafetyError):
        context[0].update_json_projection(ROOT + path, {}, "sha")
    with pytest.raises(SafetyError):
        context[0].create_append_only_record(ROOT + path, {})


def test_append_immutable_and_skip_ci(context):
    store, github, *_ = context
    path = ROOT + "data/journal/fixture.json"
    store.create_append_only_record(path, {"status": 1})
    with pytest.raises(Conflict):
        store.create_append_only_record(path, {"status": 2})
    assert store.read_file(path).json()["status"] == 1
    assert all("[skip ci]" in payload["message"] for _, payload in github.writes)


def test_projection_cas_retries_reconcile_latest(context, monkeypatch):
    store, github, *_ = context
    original = store.update_json_projection
    calls = []

    def race(path, data, expected_sha):
        calls.append(data)
        if len(calls) == 1:
            github.seed("state/current-state.json", {"foreign_field": "preserved"})
        return original(path, data, expected_sha)

    monkeypatch.setattr(store, "update_json_projection", race)
    store.merge_projection(
        "state/current-state.json", lambda latest: {**latest, "status": "ok"}
    )
    assert len(calls) == 2
    assert store.read_current_state()["foreign_field"] == "preserved"


def test_github_error_does_not_print_pat():
    def fail(_):
        raise RuntimeError("synthetic-fixture-secret")

    store = PrivateRepoStore(
        "synthetic-fixture-secret", httpx.Client(transport=httpx.MockTransport(fail))
    )
    with pytest.raises(SafetyError) as result:
        store.read_file(ROOT + "STRATEGY.md")
    assert "synthetic-fixture-secret" not in str(result.value)


def test_sdk_transport_blocks_post_and_nonpaper():
    from trading_runtime.alpaca_client import ReadOnlyPaperSession

    session = ReadOnlyPaperSession()
    with pytest.raises(SafetyError, match="PHASE1_EXECUTION_DISABLED"):
        session.post(PAPER_URL + "/v2/orders", json={})
    with pytest.raises(SafetyError, match="NOT_PAPER"):
        session.get("https://api.alpaca.markets/v2/account")


def test_durable_write_failure_prevents_submission(enabled, monkeypatch):
    context, proposal = enabled

    def unavailable(*_):
        raise SafetyError("PRIVATE_STORE_UNAVAILABLE")

    monkeypatch.setattr(context[0], "create_append_only_record", unavailable)
    with pytest.raises(SafetyError):
        submit(context, proposal)
    assert not context[2].submissions


def test_kill_changed_during_persistence_blocks_submission(enabled, monkeypatch):
    context, proposal = enabled
    store, github, broker, *_ = context
    original = store.merge_projection

    def switch_on(*args, **kwargs):
        result = original(*args, **kwargs)
        github.seed(
            "state/kill-switch.json",
            {
                "enabled": True,
                "reason": "test",
                "updated_at": None,
                "updated_by": "test",
            },
        )
        return result

    monkeypatch.setattr(store, "merge_projection", switch_on)
    assert submit(context, proposal).reason_codes == ("AUTHORITY_CHANGED",)
    assert not broker.submissions


def test_slow_persistence_revalidates_freshness(enabled, monkeypatch):
    context, proposal = enabled
    store, _, broker, *_ = context
    original = store.merge_projection

    def advance(*args, **kwargs):
        result = original(*args, **kwargs)
        broker.now = NOW + timedelta(minutes=3)
        return result

    monkeypatch.setattr(store, "merge_projection", advance)
    assert submit(context, proposal).reason_codes == ("EXECUTION_STATE_CHANGED",)
    assert not broker.submissions


def test_foreign_order_race_before_submit_blocks(enabled, monkeypatch):
    context, proposal = enabled
    store, _, broker, *_ = context
    original = store.merge_projection

    def race(*args, **kwargs):
        result = original(*args, **kwargs)
        broker._orders["foreign"] = {
            "id": "foreign",
            "client_order_id": "cobre-race",
            "symbol": "AAPL",
            "status": "new",
            "side": "buy",
            "qty": "1",
            "filled_qty": "0",
        }
        return result

    monkeypatch.setattr(store, "merge_projection", race)
    assert submit(context, proposal).decision == "REJECT"
    assert not broker.submissions


def test_unresolved_intent_no_order_does_not_allow_retry(enabled, monkeypatch):
    context, proposal = enabled
    broker = context[2]

    def uncertain(**_):
        raise TimeoutError

    monkeypatch.setattr(broker, "submit_bracket", uncertain)
    with pytest.raises(SafetyError):
        submit(context, proposal)
    assert "UNRESOLVED_ORDER_INTENT" in reconcile(broker, context[0], NOW).alerts
    assert "DUPLICATE_ORDER" in submit(context, proposal, "recovery").reason_codes


def test_projection_conflict_retry_is_bounded(context, monkeypatch):
    store = context[0]
    calls = []

    def conflict(*_):
        calls.append(1)
        raise Conflict("PRIVATE_WRITE_CONFLICT")

    monkeypatch.setattr(store, "update_json_projection", conflict)
    with pytest.raises(Conflict, match="PROJECTION_RETRY_EXHAUSTED"):
        store.merge_projection("state/current-state.json", lambda latest: latest)
    assert len(calls) == 3
