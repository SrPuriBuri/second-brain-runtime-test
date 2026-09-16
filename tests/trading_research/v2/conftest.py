"""Synthetic inputs only. The external private JSON directory contains authority, not prices."""

from datetime import datetime, timedelta
from decimal import Decimal
import os
from pathlib import Path
import socket

import pytest

from trading_research.v2.bindings import Spec
from trading_research.v2.models import Bar, NY, SyntheticSession
from trading_research.v2_protocol import UNIVERSE

SOURCE = "SYNTHETIC_GOLDEN_D1"


@pytest.fixture(scope="session")
def spec_dir():
    default = Path(__file__).resolve().parents[4] / "second-brain/projects/ai-stock-trader/research-v2/protocol-v2"
    path = Path(os.environ.get("AIST_V2_SPEC_DIR", default))
    if not (path / "protocol-v2.json").is_file():
        pytest.fail("Provide AIST_V2_SPEC_DIR with the pinned metadata-only private protocol directory.")
    return path


@pytest.fixture(scope="session")
def spec(spec_dir):
    return Spec(spec_dir)


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def blocked(*args, **kwargs):
        raise AssertionError("D1_NETWORK_FORBIDDEN")
    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)
    monkeypatch.setattr(socket, "getaddrinfo", blocked)


def at(value):
    return datetime.fromisoformat("2001-02-05T" + value).replace(tzinfo=NY)


def bar(time, o="100", h="100.1", lo="99.9", c="100", v="100000"):
    return Bar(at(time) if isinstance(time, str) else time, o, h, lo, c, v)


def session(*, replacements=None, omit=(), halts=(), excluded=(), close="16:00",
            invalid_slots=(), panel_bars=None):
    rows = {}
    for symbol in UNIVERSE:
        rows[symbol] = []
        time = at("09:30")
        while time < at(close):
            if (symbol, time) not in omit:
                rows[symbol].append((replacements or {}).get((symbol, time), bar(time)))
            time += timedelta(minutes=5)
    if panel_bars is not None:
        rows = panel_bars
    return SyntheticSession(source=SOURCE, opened=at("09:30"), closed=at(close),
                            bars=rows, halts=halts, excluded=excluded, invalid_slots=invalid_slots)


def halt(start, end):
    return {"start": at(start), "end": at(end), "verified": True, "source_ref": "SYNTHETIC_FIXTURE"}


def intent(symbol="DIA", due="13:05"):
    return {"symbol": symbol, "signal_at": at("13:00"), "due": at(due),
            "stop": Decimal("98.80"), "target": Decimal("102.40"),
            "feature": {"close": 100.0, "close_text": "100", "atr": .6,
                        "last_volume_text": "100000", "dollar_volume": 420000000.0}}


def feature_panel():
    symbols = {s: {"close": 100.0, "close_text": "100", "atr": .6,
                   "last_volume_text": "100000", "dollar_volume": 10000000.0,
                   "move": .01, "z": float(i), "relative_turn": .001}
               for i, s in enumerate(UNIVERSE)}
    symbols["DIA"]["z"] = -2.0
    return {"at": at("13:00"), "median": .01, "mad": .005, "symbols": symbols}


def record(r, *, symbol="DIA", day="2001-02-05", scenario="SEVERE",
           status="SCORED", time="13:15:00", **extra):
    return {"source": SOURCE, "symbol": symbol, "session": day, "stage": "development",
            "variant": "CSLC_L30", "record_type": "STRATEGY", "scenario": scenario,
            "status": status, "R": r, "integrity_violations": 0,
            "entry_timestamp": day + "T13:05:00-05:00",
            "accounting_exit_timestamp": day + "T" + time + "-05:00",
            "time_in_market_wall_minutes": 10.0, "time_in_market_tradable_minutes": 10.0,
            "volatility_regime": "NORMAL", **extra}
