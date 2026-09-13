import httpx
import pytest

from trading_research.data import HistoricalSource
from trading_research.reports import persist, safety
from trading_runtime.config import Config, SafetyError
from trading_runtime.simulation import simulation_store


def test_only_allowlisted_get_and_iex(tmp_path):
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={"bars": {"SPY": []}})

    source = HistoricalSource(
        Config("synthetic", "synthetic"),
        tmp_path,
        httpx.Client(transport=httpx.MockTransport(handler)),
        lambda _: None,
    )
    for route in ("orders", "positions", "account", "https://api.alpaca.markets"):
        with pytest.raises(SafetyError):
            source.get(route, {})
    with pytest.raises(SafetyError):
        source.get("bars", {"feed": "sip"})
    source.bars(["SPY"], "2020-01-01", "2020-02-01")
    source.bars(["SPY"], "2020-01-01", "2020-02-01")
    assert len(seen) == 1 and seen[0].method == "GET"
    assert len(source.receipts) == 2
    assert source.receipts[0] == source.receipts[1]


def test_audit_does_not_label_empty_history_available(tmp_path, monkeypatch):
    source = HistoricalSource(Config("synthetic", "synthetic"), tmp_path)
    monkeypatch.setattr(source, "bars", lambda *args: {"SPY": [], "QQQ": []})
    monkeypatch.setattr(
        source, "get", lambda route, params: [] if route == "calendar" else {"news": []}
    )
    report = source.audit_access()
    assert all(probe["status"] == "EMPTY" for probe in report["probes"])
    assert report["strategy_returns_computed"] is False


def test_pagination_follows_short_page_and_rejects_cycle(tmp_path):
    def handler(request):
        return httpx.Response(
            200, json={"bars": {"SPY": []}, "next_page_token": "loop"}
        )

    source = HistoricalSource(
        Config("synthetic", "synthetic"),
        tmp_path,
        httpx.Client(transport=httpx.MockTransport(handler)),
        lambda _: None,
    )
    with pytest.raises(SafetyError, match="PAGINATION_LOOP"):
        source.bars(["SPY"], "2020-01-01", "2020-02-01")


@pytest.mark.parametrize("name", ["../STRATEGY", "x/y", "", "x.json", "..\\x"])
def test_research_write_path(name):
    store, _ = simulation_store()
    with pytest.raises(SafetyError, match="PATH_FORBIDDEN"):
        persist(store, name, {})


def test_research_requires_all_safety_flags():
    store, github = simulation_store()
    github.seed(
        "state/readiness.json", {"execution_ready": False, "execution_enabled": False}
    )
    assert safety(store)["kill_switch_enabled"] is True
    path = persist(store, "test", {"status": "AUDIT"})
    assert "/data/evidence/research-v1/" in path
    assert all("[skip ci]" in payload["message"] for _, payload in github.writes)
    github.seed(
        "state/readiness.json", {"execution_ready": False, "execution_enabled": True}
    )
    with pytest.raises(SafetyError):
        safety(store)
