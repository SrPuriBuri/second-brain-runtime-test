"""Research-first orchestration with private durable claims and handoffs."""

from dataclasses import asdict
from datetime import datetime, timezone
from uuid import uuid4

from .config import SafetyError
from .executor import PaperExecutor
from .handoff import write_handoff
from .journal import event
from .lock import claim_slot
from .market_calendar import MADRID, NY, display_time, get_session
from .models import RunRecord
from .news import fetch_news
from .notify import notify
from .private_store import ROOT
from .reconcile import assert_flat, reconcile
from .slots import due_slots, run_id, slot_times
from .watchlist import build_watchlist


class Runner:
    def __init__(self, broker, store, data, strategist, notifier=notify):
        self.broker, self.store, self.data = broker, store, data
        self.strategist, self.notifier = strategist, notifier

    def context(self, now):
        self.broker.verify_paper()
        self.broker.account()
        strategy = self.store.read_strategy()
        schedule = self.store.required("routines/schedule.json").json()
        session = get_session(self.broker, now.astimezone(NY).date())
        return strategy, schedule, session

    def slot_status(self, now):
        strategy, schedule, session = self.context(now)
        return {
            "mode": "PAPER_ONLY",
            "strategy_version": strategy.version,
            "due": due_slots(now, session, schedule),
            "times": {
                name: display_time(at)
                for name, at in slot_times(session, schedule).items()
            },
        }

    def scheduled(self, now, requested=None):
        strategy, schedule, session = self.context(now)
        due = due_slots(now, session, schedule)
        if requested:
            due = [slot for slot in due if slot == requested]
        results = []
        for slot in due:
            day = (
                str(now.astimezone(MADRID).date())
                if slot == "MORNING_RESEARCH_ES"
                else session.trade_date
            )
            rid = run_id(day, slot, strategy.version)
            record = RunRecord(
                run_id=rid,
                trade_date=day,
                slot=slot,
                strategy_version=strategy.version,
                timestamp=now,
                status="CLAIMED",
            )
            if claim_slot(self.store, day, slot, record.model_dump(mode="json")):
                results.append(
                    self.perform(slot, rid, now, strategy, schedule, session)
                )
        return {"status": "COMPLETE" if results else "NO_OP", "runs": results}

    def manual(self, command, now):
        strategy, schedule, session = self.context(now)
        rid = run_id(
            str(now.astimezone(NY).date()),
            f"MANUAL_{command.upper()}_{uuid4().hex[:12]}",
            strategy.version,
        )
        return self.perform(command, rid, now, strategy, schedule, session)

    def perform(self, slot, rid, now, strategy, schedule, session):
        refs, unresolved, status, candidates = [], [], "COMPLETE", 0
        # The previous handoff is explicitly read; model memory is not continuity.
        previous = self.store.read_current_state()
        if previous.get("handoff"):
            self.store.read_file(previous["handoff"])
        try:
            snapshot = reconcile(self.broker, self.store, now)
            unresolved.extend(snapshot.alerts)
            refs.append(event(self.store, rid, "reconciliation", now, asdict(snapshot)))
            if slot in {
                "PREP",
                "FIRST_SCAN",
                "LAST_NEW_TRADE",
                "MORNING_RESEARCH_ES",
                "research",
            }:
                policy = self.store.read_policy()
                watchlist = build_watchlist(
                    self.broker.assets(),
                    self.data,
                    policy,
                    strategy,
                    now,
                    session.trade_date if session else str(now.date()),
                )
                candidates = len(watchlist.candidates)
                path = ROOT + f"data/watchlists/{rid}.json"
                self.store.create_append_only_record(
                    path, watchlist.model_dump(mode="json")
                )
                refs.append(path)
                news, notes = fetch_news(
                    self.data, [c["symbol"] for c in watchlist.candidates]
                )
                refs.append(
                    event(self.store, rid, "news", now, {"items": news, "notes": notes})
                )
                proposals = self.strategist.propose(
                    {
                        "watchlist": watchlist.model_dump(mode="json"),
                        "news": news,
                        "account_evidence": {
                            "shared_account": True,
                            "reconciled": snapshot.reconciled,
                            "virtual_risk_equity": policy.virtual_risk_equity,
                            "available_cash": snapshot.account["cash"],
                            "buying_power": snapshot.account["buying_power"],
                            "project_position_count": snapshot.position_count,
                            "realized_loss": str(snapshot.realized_loss),
                            "estimated_open_risk": str(snapshot.open_risk),
                        },
                    },
                    self.store.required("STRATEGY.md").text,
                )
                # No frozen strategy implementation exists in Phase 1.
                executor = PaperExecutor(
                    self.broker,
                    self.store,
                    lambda symbol, p: (
                        next(
                            (c for c in watchlist.candidates if c["symbol"] == symbol),
                            None,
                        ),
                        symbol in [c["symbol"] for c in watchlist.candidates],
                    ),
                    lambda *_: False,
                )
                for proposal in proposals:
                    if slot in {"FIRST_SCAN", "LAST_NEW_TRADE"}:
                        decision = executor.submit(
                            proposal.model_dump(), rid, slot, now, session, schedule
                        )
                        unresolved.extend(decision.reason_codes)
                    else:
                        refs.append(
                            event(
                                self.store,
                                rid,
                                "research_proposal",
                                now,
                                proposal.model_dump(mode="json"),
                            )
                        )
                if not strategy.tradable:
                    refs.append(
                        event(
                            self.store,
                            rid,
                            "research_only",
                            now,
                            {
                                "reason_codes": [
                                    "STRATEGY_NOT_TRADABLE",
                                    "PHASE1_EXECUTION_DISABLED",
                                ]
                            },
                        )
                    )
            elif slot == "FORCE_FLAT":
                executor = PaperExecutor(self.broker, self.store, None, None)
                unresolved.extend(executor.force_flat(rid, now))
            elif slot in {"RECONCILE", "POST_CLOSE_REPORT", "report", "reconcile"}:
                if slot != "reconcile" or (
                    session and now >= slot_times(session, schedule)["FORCE_FLAT"]
                ):
                    unresolved.extend(assert_flat(snapshot))
                if slot == "POST_CLOSE_REPORT" and session:
                    for required in ("FORCE_FLAT", "RECONCILE"):
                        if (
                            self.store.read_file(
                                ROOT
                                + f"data/evidence/claims/{session.trade_date}_{required}.json"
                            )
                            is None
                        ):
                            unresolved.append("MISSED_" + required)
            elif slot == "MANAGE":
                refs.append(
                    event(
                        self.store,
                        rid,
                        "management_read_only",
                        now,
                        {"reason": "PHASE1_EXECUTION_DISABLED"},
                    )
                )
            else:
                raise SafetyError("UNKNOWN_SLOT")
        except Exception as exc:
            status = "FAILED"
            code = str(exc) if isinstance(exc, SafetyError) else "RUNTIME_FAILED"
            unresolved.append(code)
            refs.append(
                event(self.store, rid, "runtime_failure", now, {"reason_codes": [code]})
            )
        unresolved = sorted(set(unresolved))
        future_slots = sorted(
            (at, name) for name, at in slot_times(session, schedule).items() if at > now
        )
        next_action = (
            (
                f"Next slot: {future_slots[0][1]} at {display_time(future_slots[0][0])}. "
                if future_slots
                else "Next: informational morning research at 08:10 Europe/Madrid on the next weekday; refresh Alpaca calendar before the next session. "
            )
            + "Review unresolved issues and reconcile exact broker IDs; keep strategy and kill switch disabled."
        )
        handoff = write_handoff(
            self.store, rid, now, status, refs, unresolved, next_action
        )

        def project(latest):
            # Preserve unrelated fields; never replace newer state with older run.
            if latest.get("updated_at") and latest["updated_at"] > now.isoformat():
                raise SafetyError("PROJECTION_NEWER_THAN_RUN")
            return {
                **latest,
                "last_run_id": rid,
                "updated_at": now.isoformat(),
                "status": status,
                "unresolved": unresolved,
                "handoff": handoff,
            }

        self.store.merge_projection("state/current-state.json", project)
        record = RunRecord(
            run_id=rid,
            trade_date=now.astimezone(NY).date(),
            slot=slot,
            strategy_version=strategy.version,
            timestamp=now,
            status=status,
            evidence_refs=refs + [handoff],
            reason_codes=unresolved,
        )
        self.store.create_append_only_record(
            ROOT + f"data/progress/{rid}.json", record.model_dump(mode="json")
        )
        summary = {
            "run_id": rid,
            "status": status,
            "candidate_count": candidates,
            "alert_count": len(unresolved),
            "attention_required": bool(unresolved),
            "mode": "PAPER_ONLY",
            "time": display_time(now),
        }
        # Commit completion before notification: notification failure cannot replay work.
        failures = self.notifier(summary)
        if failures:
            event(
                self.store, rid, "notification_failure", now, {"reason_codes": failures}
            )
        return summary


def utc_now():
    return datetime.now(timezone.utc)
