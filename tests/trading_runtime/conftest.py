import socket

import pytest

from trading_runtime.executor import PaperExecutor
from trading_runtime.market_calendar import get_session
from trading_runtime.simulation import (
    NOW,
    FakeAlpaca,
    sample_proposal,
    simulation_store,
)


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def blocked(*args, **kwargs):
        raise AssertionError("Tests must never contact a real service")

    monkeypatch.setattr(socket.socket, "connect", blocked)


@pytest.fixture
def context():
    store, github = simulation_store()
    broker = FakeAlpaca()
    schedule = store.required("routines/schedule.json").json()
    session = get_session(broker, NOW.date())
    evidence = {"ask": 100, "timestamp": NOW.isoformat(), "feed": "iex"}
    executor = PaperExecutor(
        broker, store, lambda *_: (evidence, True), lambda *_: True
    )
    return store, github, broker, schedule, session, evidence, executor


@pytest.fixture
def enabled(context):
    store, github, *_ = context
    github.seed(
        "STRATEGY.md",
        '```json\n{"status":"FROZEN","version":1,"tradable":true,"implementation":"TEST_ONLY"}\n```',
    )
    github.seed(
        "state/kill-switch.json",
        {
            "enabled": False,
            "reason": "in-memory test only",
            "updated_at": None,
            "updated_by": "test",
        },
    )
    proposal = sample_proposal()
    proposal["strategy_version"] = 1
    return context, proposal
