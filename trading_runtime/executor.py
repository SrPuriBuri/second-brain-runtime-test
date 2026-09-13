"""Revalidate every proposal; production adapter independently blocks mutations."""

import hashlib
from dataclasses import asdict
from uuid import uuid4

from pydantic import ValidationError

from .config import SafetyError
from .journal import event
from .market_calendar import aware
from .models import TradeProposal
from .policy import PolicyDecision, evaluate
from .private_store import ROOT, Conflict
from .reconcile import assert_flat, flatten, reconcile
from .risk import dec
from .slots import due_slots


def client_order_id(proposal):
    key = f"{proposal.strategy_version}|{proposal.trade_date}|{proposal.symbol}|BUY|{proposal.proposal_id}"
    return "aist-" + hashlib.sha256(key.encode()).hexdigest()[:40]


class PaperExecutor:
    def __init__(self, broker, store, evidence_source, qualification):
        self.broker, self.store = broker, store
        # These are trusted runtime dependencies; the strategist sees neither.
        self.evidence_source, self.qualification = evidence_source, qualification

    def submit(self, raw, run, slot, now, session, schedule):
        try:
            proposal = TradeProposal.model_validate(raw)
        except (ValidationError, ValueError, TypeError):
            decision = PolicyDecision("REJECT", ("MALFORMED_PROPOSAL",))
            event(self.store, run, "rejection", now, decision.__dict__)
            return decision
        try:
            self.broker.verify_paper()
            strategy, policy, kill = (
                self.store.read_strategy(),
                self.store.read_policy(),
                self.store.read_kill_switch(),
            )
            snapshot = reconcile(self.broker, self.store, now)
            cid = client_order_id(proposal)
            prior = self.broker.order_by_client_id(cid)
            path = ROOT + f"data/evidence/orders/{cid}/intent.json"
            evidence, allowed = self.evidence_source(proposal.symbol, policy)
            decision = evaluate(
                proposal,
                policy,
                strategy,
                kill,
                snapshot,
                session,
                schedule,
                slot,
                now,
                self.broker.clock(),
                evidence,
                allowed,
                prior is not None or self.store.read_file(path) is not None,
                self.qualification(strategy, proposal, evidence),
            )
        except Exception as exc:
            # No SDK/validation exception text can leak private values.
            code = (
                str(exc)
                if isinstance(exc, SafetyError)
                else "EXECUTION_PREREQUISITE_FAILED"
            )
            decision = PolicyDecision("REJECT", (code,))
        event(self.store, run, "policy", now, decision.__dict__)
        if decision.decision != "PASS":
            event(self.store, run, "rejection", now, decision.__dict__)
            self.store.create_append_only_record(
                ROOT + f"data/rejections/{run}/{uuid4().hex}.json", decision.__dict__
            )
            return decision
        entry = {
            "account_id": snapshot.account["id"],
            "client_order_id": cid,
            "symbol": proposal.symbol,
            "side": "BUY",
            "quantity": decision.quantity,
            "entry_price": proposal.entry_price_or_rule,
            "stop_price": proposal.stop_price,
            "target_price": proposal.target_price,
            "trade_date": str(proposal.trade_date),
            "strategy_version": proposal.strategy_version,
            "run_id": run,
        }
        try:
            self.store.create_append_only_record(path, entry)
        except Conflict:
            event(
                self.store, run, "rejection", now, {"reason_codes": ["DUPLICATE_ORDER"]}
            )
            return PolicyDecision("REJECT", ("DUPLICATE_ORDER",))

        def reserve(ledger):
            if ledger["account_id"] != entry["account_id"] or cid in ledger["orders"]:
                raise SafetyError("OWNERSHIP_RESERVATION_CONFLICT")
            ledger["orders"][cid] = entry
            return ledger

        self.store.merge_projection("state/ownership-ledger.json", reserve)
        # Final authority reread after potentially slow durable writes.
        if (
            self.store.read_kill_switch().enabled
            or self.store.read_strategy() != strategy
            or self.store.read_policy() != policy
        ):
            event(
                self.store,
                run,
                "rejection",
                now,
                {"reason_codes": ["AUTHORITY_CHANGED"]},
            )
            return PolicyDecision("REJECT", ("AUTHORITY_CHANGED",))
        # Store writes may take seconds. Recheck real broker time, quote and account
        # immediately before the mutation boundary; never trust a frozen caller time.
        clock = self.broker.clock()
        current_time = aware(clock["timestamp"])
        fresh_evidence, allowed = self.evidence_source(proposal.symbol, policy)
        fresh_account = self.broker.account()
        if (
            current_time < now
            or not clock["is_open"]
            or slot not in due_slots(current_time, session, schedule)
            or current_time >= proposal.expires_at
            or not allowed
            or fresh_evidence is None
            or not 0
            <= (current_time - aware(fresh_evidence["timestamp"])).total_seconds()
            <= policy.max_data_age_seconds
            or not 0
            <= (current_time - proposal.data_timestamp).total_seconds()
            <= policy.max_data_age_seconds
            or fresh_evidence["feed"] != proposal.data_feed
            or fresh_evidence["ask"] > proposal.max_entry_price
            or fresh_account["id"] != snapshot.account["id"]
            or dec(fresh_account["cash"])
            < dec(entry["entry_price"]) * decision.quantity
            or dec(fresh_account["buying_power"])
            < dec(entry["entry_price"]) * decision.quantity
            or self.broker.positions() != snapshot.positions
            or [
                o
                for o in flatten(self.broker.open_orders())
                if o["status"]
                not in {"filled", "canceled", "expired", "rejected", "replaced"}
            ]
            != snapshot.open_orders
            or self.broker.order_by_client_id(cid) is not None
        ):
            event(
                self.store,
                run,
                "rejection",
                current_time,
                {"reason_codes": ["EXECUTION_STATE_CHANGED"]},
            )
            return PolicyDecision("REJECT", ("EXECUTION_STATE_CHANGED",))
        try:
            result = self.broker.submit_bracket(
                symbol=proposal.symbol,
                quantity=decision.quantity,
                limit_price=proposal.entry_price_or_rule,
                stop_price=proposal.stop_price,
                target_price=proposal.target_price,
                client_order_id=cid,
                expires_at=proposal.expires_at.isoformat(),
            )
        except Exception:
            event(
                self.store,
                run,
                "submission_uncertain",
                now,
                {
                    "client_order_id": cid,
                    "next_action": "Reconcile exact client ID; never automatically resubmit.",
                },
            )
            raise SafetyError("SUBMISSION_UNCERTAIN_OR_DISABLED") from None
        self.store.create_append_only_record(
            ROOT + f"data/evidence/orders/{cid}/ack.json", result
        )
        after = reconcile(self.broker, self.store, now)
        event(
            self.store,
            run,
            "paper_order_accepted",
            now,
            {"client_order_id": cid, "alerts": after.alerts},
        )
        return decision

    def force_flat(self, run, now):
        # Risk-reducing operations do not need AI or kill-switch release.
        self.store.read_kill_switch()  # Must be readable; enabled does not block exits.
        before = reconcile(self.broker, self.store, now)
        if before.alerts:
            event(
                self.store,
                run,
                "high_severity_reconciliation",
                now,
                {"alerts": before.alerts},
            )
            return before.alerts
        try:
            for order in before.owned_orders:
                event(self.store, run, "cancel_intent", now, {"order_id": order["id"]})
                self.broker.cancel_order(order["id"])
            current = reconcile(self.broker, self.store, now)
            if current.alerts or current.owned_orders:
                raise SafetyError("CANCELLATION_NOT_CONFIRMED")
            for symbol, quantity in current.owned_positions.items():
                # A deterministic exit has its own intent and ownership reservation.
                cid = (
                    "aist-"
                    + hashlib.sha256(f"{run}|{symbol}|FLAT".encode()).hexdigest()[:40]
                )
                existing = self.broker.order_by_client_id(cid)
                if existing:
                    continue
                ledger = self.store.required("state/ownership-ledger.json").json()
                bases = [
                    dec(o.get("filled_avg_price"))
                    for o in current.orders.values()
                    if o["symbol"] == symbol
                    and o["side"] == "buy"
                    and o.get("filled_avg_price")
                ]
                if len(bases) != 1:
                    raise SafetyError("EXIT_BASIS_AMBIGUOUS")
                intent = {
                    "account_id": current.account["id"],
                    "client_order_id": cid,
                    "symbol": symbol,
                    "side": "SELL",
                    "quantity": str(quantity),
                    "entry_basis": str(bases[0]),
                    "trade_date": now.date().isoformat(),
                    "run_id": run,
                }
                self.store.create_append_only_record(
                    ROOT + f"data/evidence/orders/{cid}/intent.json", intent
                )

                def reserve(latest):
                    if latest != ledger:
                        raise SafetyError("OWNERSHIP_CHANGED_DURING_EXIT")
                    latest["orders"][cid] = intent
                    return latest

                self.store.merge_projection("state/ownership-ledger.json", reserve)
                self.broker.close_owned(symbol, quantity, cid)
        except Exception:
            event(
                self.store,
                run,
                "force_flat_failure",
                now,
                {"reason_codes": ["FORCE_FLAT_UNRESOLVED"]},
            )
            return ["FORCE_FLAT_UNRESOLVED"]
        after = reconcile(self.broker, self.store, now)
        issues = assert_flat(after)
        event(
            self.store,
            run,
            "force_flat_result",
            now,
            {"unresolved": issues, "snapshot": asdict(after)},
        )
        return issues
