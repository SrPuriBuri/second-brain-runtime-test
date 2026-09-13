"""Operational commands. dry-run is always an offline simulation."""

import argparse
import json
import os
from collections import Counter

from .alpaca_client import PaperAlpaca
from .config import Config, SafetyError, redact
from .market_calendar import NY, get_session
from .market_data import MarketData
from .notify import notify
from .private_store import PrivateRepoStore, ROOT
from .runner import Runner, utc_now
from .strategist import provider


def inventory(broker, data, now):
    broker.verify_paper()
    account = broker.account()
    assets = broker.assets()
    tradable = [
        a
        for a in assets
        if a.get("status") == "active"
        and a.get("tradable")
        and a.get("class") == "us_equity"
    ]
    _, feed, notes = data.snapshots(["SPY"], "iex")
    session = get_session(broker, now.astimezone(NY).date())
    return {
        "mode": "PAPER_ONLY",
        "paper_verified": True,
        "account_status": account["status"],
        "account_id": account["id"],
        "equity": account["equity"],
        "cash": account["cash"],
        "buying_power": account["buying_power"],
        "shared_account": True,
        "feed_tested": feed,
        "sip_entitlement": "unknown",
        "notes": notes,
        "active_tradable_count": len(tradable),
        "sample_symbols": [a["symbol"] for a in tradable[:15]],
        "fractionable_count": sum(bool(a.get("fractionable")) for a in tradable),
        "exchanges": dict(Counter(a["exchange"] for a in tradable)),
        "clock": broker.clock(),
        "session": {
            "date": session.trade_date,
            "open": session.open.isoformat(),
            "close": session.close.isoformat(),
        }
        if session
        else None,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command",
        choices=[
            "connectivity",
            "inventory",
            "slot-status",
            "research",
            "dry-run",
            "reconcile",
            "run-slot",
            "report",
            "scheduled",
        ],
    )
    parser.add_argument(
        "--slot",
        choices=[
            "PREP",
            "FIRST_SCAN",
            "LAST_NEW_TRADE",
            "MANAGE",
            "FORCE_FLAT",
            "RECONCILE",
            "POST_CLOSE_REPORT",
            "MORNING_RESEARCH_ES",
        ],
    )
    parser.add_argument(
        "--fake",
        action="store_true",
        help="Only valid for dry-run; never uses credentials",
    )
    args = parser.parse_args(argv)
    if args.command == "run-slot" and args.slot is None:
        parser.error("run-slot requires --slot")
    if args.fake and args.command != "dry-run":
        parser.error("--fake is only available for dry-run")
    try:
        if args.command == "dry-run":
            from .simulation import dry_run

            print(json.dumps(dry_run()))
            return 0
        config = Config.from_env()
        broker = PaperAlpaca(config)
        now = utc_now()
        if args.command == "connectivity":
            notify(
                {
                    "mode": "PAPER_ONLY",
                    "account_status": broker.account()["status"],
                    "execution": "PHASE1_DISABLED",
                }
            )
            return 0
        data = MarketData(config)
        store = PrivateRepoStore(config.pat)
        if args.command == "inventory":
            result = inventory(broker, data, now)
            store.create_append_only_record(
                ROOT
                + f"data/evidence/inventory/{now.strftime('%Y%m%dT%H%M%S%f')}.json",
                result,
            )
            if os.getenv("GITHUB_ACTIONS") == "true":
                notify(
                    {"mode": "PAPER_ONLY", "status": "INVENTORY_PERSISTED_PRIVATELY"}
                )
            else:
                print(redact(json.dumps(result)))
            return 0
        runner = Runner(broker, store, data, provider(config))
        if args.command == "slot-status":
            print(json.dumps(runner.slot_status(now)))
        elif args.command in {"scheduled", "run-slot"}:
            result = runner.scheduled(now, args.slot)
            return int(any(r["status"] == "FAILED" for r in result["runs"]))
        else:
            result = runner.manual(args.command, now)
            return int(result["status"] == "FAILED")
        return 0
    except Exception as exc:
        code = str(exc) if isinstance(exc, SafetyError) else "RUNTIME_FAILED"
        notify({"status": "FAILED", "reason": redact(code)})
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
