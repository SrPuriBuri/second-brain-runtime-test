"""Entirely in-memory broker and GitHub transport, never reads credentials."""

import base64
import copy
import hashlib
import json
from datetime import datetime, timedelta, timezone

import httpx

from .config import PAPER_URL, SafetyError
from .executor import PaperExecutor
from .market_calendar import get_session
from .models import Policy, Strategy
from .private_store import API, ROOT, PrivateRepoStore
from .risk import dec
from .runner import Runner
from .strategist import NoAI

NOW = datetime(2026, 9, 10, 14, 30, tzinfo=timezone.utc)


class MemoryGitHub:
    def __init__(self):
        self.files, self.writes = {}, []

    def seed(self, path, data):
        text = data if isinstance(data, str) else json.dumps(data)
        self.files[ROOT + path] = (text, hashlib.sha256(text.encode()).hexdigest())

    def handle(self, request):
        path = str(request.url)[len(API) :]
        current = self.files.get(path)
        if request.method == "GET":
            if current is None:
                return httpx.Response(404)
            return httpx.Response(
                200,
                json={
                    "type": "file",
                    "content": base64.b64encode(current[0].encode()).decode(),
                    "sha": current[1],
                },
            )
        body = json.loads(request.content)
        self.writes.append((path, body))
        if (current and body.get("sha") != current[1]) or (
            current is None and body.get("sha")
        ):
            return httpx.Response(409)
        text = base64.b64decode(body["content"]).decode()
        sha = hashlib.sha256(text.encode()).hexdigest()
        self.files[path] = (text, sha)
        return httpx.Response(200 if current else 201, json={"content": {"sha": sha}})


def simulation_store():
    github = MemoryGitHub()
    github.seed(
        "STRATEGY.md",
        "```json\n"
        + Strategy(
            status="RESEARCH_REQUIRED", version=0, tradable=False
        ).model_dump_json()
        + "\n```",
    )
    github.seed("POLICY.md", "```json\n" + Policy().model_dump_json() + "\n```")
    github.seed(
        "state/kill-switch.json",
        {
            "enabled": True,
            "reason": "simulation bootstrap",
            "updated_at": None,
            "updated_by": "simulation",
        },
    )
    github.seed("state/current-state.json", {"last_run_id": None, "updated_at": None})
    github.seed(
        "state/ownership-ledger.json",
        {
            "schema_version": 1,
            "account_id": "SIMULATED_ACCOUNT",
            "orders": {},
            "updated_at": None,
        },
    )
    slots = {
        "PREP": ("open", -45),
        "FIRST_SCAN": ("open", 60),
        "LAST_NEW_TRADE": ("open", 150),
        "MANAGE": ("close", -120),
        "FORCE_FLAT": ("close", -45),
        "RECONCILE": ("close", -15),
        "POST_CLOSE_REPORT": ("close", 15),
    }
    github.seed(
        "routines/schedule.json",
        {
            "due_window_minutes": 20,
            "slots": {
                name: {"anchor": anchor, "offset_minutes": offset}
                for name, (anchor, offset) in slots.items()
            },
            "MORNING_RESEARCH_ES": {"time": "08:10", "timezone": "Europe/Madrid"},
        },
    )
    store = PrivateRepoStore(
        "synthetic-fixture", httpx.Client(transport=httpx.MockTransport(github.handle))
    )
    return store, github


class FakeAlpaca:
    def __init__(self):
        self.now = NOW
        self._orders, self._positions = {}, {}
        self.submissions, self.cancellations, self.closures = [], [], []
        self.crash_after_submit = False
        self.close_time = "16:00"
        self.is_open = True
        self.account_data = {
            "id": "SIMULATED_ACCOUNT",
            "status": "ACTIVE",
            "equity": "10000",
            "cash": "10000",
            "buying_power": "20000",
        }

    def verify_paper(self):
        return PAPER_URL

    def account(self):
        return copy.deepcopy(self.account_data)

    def clock(self):
        return {"timestamp": self.now.isoformat(), "is_open": self.is_open}

    def calendar(self, day):
        return (
            []
            if day.weekday() > 4
            else [{"date": str(day), "open": "09:30", "close": self.close_time}]
        )

    def assets(self):
        return [
            {
                "symbol": symbol,
                "name": symbol,
                "class": "us_equity",
                "exchange": "NASDAQ",
                "status": "active",
                "tradable": True,
                "fractionable": True,
            }
            for symbol in Policy().seeds
        ]

    def positions(self):
        return [
            {"symbol": symbol, "qty": str(qty)}
            for symbol, qty in self._positions.items()
            if qty
        ]

    def open_orders(self):
        terminal = {"filled", "canceled", "rejected", "expired"}
        return [
            copy.deepcopy(o)
            for o in self._orders.values()
            if o["status"] not in terminal
            or any(leg["status"] not in terminal for leg in o.get("legs", []))
        ]

    def order_by_client_id(self, cid):
        return copy.deepcopy(self._orders.get(cid))

    def submit_bracket(self, **kwargs):
        cid, symbol = kwargs["client_order_id"], kwargs["symbol"]
        if cid in self._orders:
            raise SafetyError("DUPLICATE_ORDER")
        self.submissions.append(kwargs)
        order = {
            "id": cid + "-broker",
            "client_order_id": cid,
            "symbol": symbol,
            "side": "buy",
            "qty": str(kwargs["quantity"]),
            "filled_qty": str(kwargs["quantity"]),
            "filled_avg_price": str(kwargs["limit_price"]),
            "filled_at": self.now.isoformat(),
            "status": "filled",
            "legs": [],
        }
        for kind in ("stop", "target"):
            order["legs"].append(
                {
                    "id": cid + "-" + kind,
                    "client_order_id": "broker-generated-" + kind,
                    "symbol": symbol,
                    "side": "sell",
                    "qty": str(kwargs["quantity"]),
                    "filled_qty": "0",
                    "status": "new",
                }
            )
        self._orders[cid] = order
        self._positions[symbol] = self._positions.get(symbol, 0) + kwargs["quantity"]
        if self.crash_after_submit:
            raise TimeoutError("synthetic timeout after accepted order")
        return copy.deepcopy(order)

    def cancel_order(self, order_id):
        self.cancellations.append(order_id)
        for order in self._orders.values():
            for member in [order] + order.get("legs", []):
                if member["id"] == order_id:
                    member["status"] = "canceled"

    def close_owned(self, symbol, quantity, client_order_id):
        self.closures.append(symbol)
        if dec(self._positions[symbol]) != quantity:
            raise SafetyError("AMBIGUOUS_OWNERSHIP")
        self._positions[symbol] = 0
        self._orders[client_order_id] = {
            "id": client_order_id + "-broker",
            "client_order_id": client_order_id,
            "symbol": symbol,
            "side": "sell",
            "qty": str(quantity),
            "filled_qty": str(quantity),
            "filled_avg_price": "100",
            "filled_at": self.now.isoformat(),
            "status": "filled",
            "legs": [],
        }


class FakeData:
    def __init__(self, now=NOW):
        self.now = now

    def snapshots(self, symbols, feed="iex"):
        return (
            {
                s: {
                    "latestQuote": {"bp": 99.99, "ap": 100, "t": self.now.isoformat()},
                    "prevDailyBar": {
                        "v": 1000000,
                        "c": 100,
                        "t": (self.now - timedelta(days=1)).isoformat(),
                    },
                }
                for s in symbols
            },
            feed,
            [],
        )

    def get(self, *_):
        return httpx.Response(200, json={"news": []})


def sample_proposal():
    return {
        "proposal_id": "fixture-setup",
        "strategy_version": 0,
        "trade_date": "2026-09-10",
        "generated_at": NOW.isoformat(),
        "symbol": "AAPL",
        "side": "BUY",
        "thesis": "Synthetic fixture, not a strategy",
        "evidence_refs": ["simulation/quote"],
        "entry_type": "LIMIT",
        "entry_price_or_rule": 100,
        "stop_price": 98,
        "target_price": 104,
        "max_entry_price": 100,
        "expires_at": (NOW + timedelta(hours=1)).isoformat(),
        "confidence": 0.5,
        "risk_notes": ["Synthetic data"],
        "data_timestamp": NOW.isoformat(),
        "data_feed": "iex",
    }


def dry_run():
    broker = FakeAlpaca()
    store, github = simulation_store()
    runner = Runner(broker, store, FakeData(), NoAI(), notifier=lambda _: [])
    first = runner.scheduled(NOW, "FIRST_SCAN")
    second = runner.scheduled(NOW, "FIRST_SCAN")
    executor = PaperExecutor(
        broker,
        store,
        lambda *_: ({"ask": 100, "timestamp": NOW.isoformat(), "feed": "iex"}, True),
        lambda *_: False,
    )
    decision = executor.submit(
        sample_proposal(),
        "simulation-gate",
        "FIRST_SCAN",
        NOW,
        get_session(broker, NOW.date()),
        store.required("routines/schedule.json").json(),
    )
    if (
        first["status"] != "COMPLETE"
        or second["status"] != "NO_OP"
        or broker.submissions
        or decision.decision != "REJECT"
    ):
        raise SafetyError("SIMULATION_FAILED")
    return {
        "status": "PASS",
        "mode": "OFFLINE_SIMULATION",
        "real_network_calls": 0,
        "orders_submitted": len(broker.submissions),
        "duplicate_slot": second["status"],
        "gate_reasons": decision.reason_codes,
        "durable_simulated_files": len(github.files),
    }
