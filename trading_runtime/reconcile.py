"""Broker-verified ownership from immutable intents and exact recorded order IDs."""

from dataclasses import dataclass
from decimal import Decimal

from .market_calendar import aware
from .private_store import ROOT
from .risk import dec


def flatten(orders):
    result = []
    for order in orders:
        result.append(order)
        result.extend(flatten(order.get("legs") or []))
    return result


@dataclass
class Snapshot:
    account: dict
    positions: list
    open_orders: list
    owned_orders: list
    owned_positions: dict
    orders: dict
    alerts: list
    new_positions: int
    position_count: int
    realized_loss: Decimal
    open_risk: Decimal

    @property
    def reconciled(self):
        return not self.alerts


def reconcile(broker, store, now):
    broker.verify_paper()
    account = broker.account()
    positions = broker.positions()
    terminal = {"filled", "canceled", "expired", "rejected", "replaced"}
    open_orders = list(
        {
            o["id"]: o
            for o in flatten(broker.open_orders())
            if o["status"] not in terminal
        }.values()
    )
    ledger = store.required("state/ownership-ledger.json").json()
    alerts, verified, quantities, known_ids, costs = [], {}, {}, set(), {}
    daily_new, loss, open_risk = 0, Decimal(0), Decimal(0)
    active_symbols = set()
    if ledger.get("account_id") != account["id"]:
        alerts.append("ACCOUNT_NOT_BOUND")
    for cid, entry in ledger["orders"].items():
        if not cid.startswith("aist-") or entry.get("account_id") != account["id"]:
            alerts.append("INVALID_OWNERSHIP_LEDGER")
            continue
        intent = store.read_file(ROOT + f"data/evidence/orders/{cid}/intent.json")
        if intent is None or intent.json() != entry:
            alerts.append("OWNERSHIP_INTENT_MISMATCH")
            continue
        order = broker.order_by_client_id(cid)
        if order is None:
            alerts.append("UNRESOLVED_ORDER_INTENT")
            continue
        if (
            order.get("client_order_id") != cid
            or order.get("symbol") != entry["symbol"]
            or order.get("side") != entry["side"].lower()
            or dec(order["qty"]) != dec(entry["quantity"])
        ):
            alerts.append("ORDER_IDENTITY_MISMATCH")
            continue
        verified[cid] = order
        members = flatten([order])
        known_ids.update(o["id"] for o in members)
        symbol = entry["symbol"]
        qty = dec(order.get("filled_qty", 0))
        signed = qty if entry["side"] == "BUY" else -qty
        quantities[symbol] = quantities.get(symbol, Decimal(0)) + signed
        if entry["side"] == "BUY":
            costs[symbol] = dec(order.get("filled_avg_price") or entry["entry_price"])
            if entry["trade_date"] == now.date().isoformat():
                daily_new += 1  # conservatively count accepted/canceled attempts
            remaining = qty
            for child in members[1:]:
                if child.get("symbol") != symbol or child.get("side") != "sell":
                    alerts.append("INVALID_BRACKET_OWNERSHIP")
                    continue
                sold = dec(child.get("filled_qty", 0))
                remaining -= sold
                quantities[symbol] -= sold
                if (
                    sold
                    and child.get("filled_at")
                    and aware(child["filled_at"]).date() == now.date()
                ):
                    loss += (
                        max(Decimal(0), costs[symbol] - dec(child["filled_avg_price"]))
                        * sold
                    )
            pending = (
                max(Decimal(0), dec(entry["quantity"]) - qty)
                if order["status"] not in {"filled", "canceled", "expired", "rejected"}
                else Decimal(0)
            )
            if remaining > 0 or pending > 0:
                active_symbols.add(symbol)
                open_risk += (remaining + pending) * max(
                    Decimal(0), dec(entry["entry_price"]) - dec(entry["stop_price"])
                )
    # Separate project exits link to original entry basis. Missing basis fails closed.
    for cid, order in verified.items():
        if order["side"] == "sell" and dec(order.get("filled_qty", 0)):
            entry = ledger["orders"][cid]
            if "entry_basis" not in entry:
                alerts.append("EXIT_BASIS_UNKNOWN")
            elif (
                order.get("filled_at")
                and aware(order["filled_at"]).date() == now.date()
            ):
                loss += max(
                    Decimal(0),
                    dec(entry["entry_basis"]) - dec(order["filled_avg_price"]),
                ) * dec(order["filled_qty"])
    # Verify quantities against the account's net positions; never infer from prefix.
    by_symbol = {p["symbol"]: dec(p["qty"]) for p in positions}
    owned = {}
    foreign_symbols = {o["symbol"] for o in open_orders if o["id"] not in known_ids}
    for symbol, qty in quantities.items():
        if qty < 0 or (
            qty != 0 and (by_symbol.get(symbol) != qty or symbol in foreign_symbols)
        ):
            alerts.append(f"AMBIGUOUS_OWNERSHIP:{symbol}")
        elif qty > 0:
            owned[symbol] = qty
        elif by_symbol.get(symbol, 0) != 0:
            # Project ledger is flat; remaining account exposure is foreign.
            # Do not claim or close it.
            continue
    for order in open_orders:
        if (
            order.get("client_order_id", "").startswith("aist-")
            and order["id"] not in known_ids
        ):
            alerts.append("UNKNOWN_PROJECT_ORDER")
    owned_orders = [o for o in open_orders if o["id"] in known_ids]
    return Snapshot(
        account,
        positions,
        open_orders,
        owned_orders,
        owned,
        verified,
        sorted(set(alerts)),
        daily_new,
        len(active_symbols | set(owned)),
        loss,
        open_risk,
    )


def assert_flat(snapshot):
    issues = list(snapshot.alerts)
    if snapshot.owned_positions:
        issues.append("PROJECT_POSITIONS_REMAIN")
    if snapshot.owned_orders:
        issues.append("PROJECT_ORDERS_REMAIN")
    return issues
