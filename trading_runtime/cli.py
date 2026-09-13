"""Operational commands. dry-run is always an offline simulation."""

import argparse
import json
import os

from .alpaca_client import PaperAlpaca
from .config import Config, SafetyError, redact
from .diagnostics import assert_validation_safety, run_diagnostics
from .market_data import MarketData
from .notify import notify
from .private_store import PrivateRepoStore
from .runner import Runner, utc_now
from .strategist import provider


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command",
        choices=[
            "connectivity",
            "inventory",
            "validate-paper",
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
        if args.command in {"inventory", "validate-paper"}:
            assert_validation_safety(PrivateRepoStore(config.pat))
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
        if args.command in {"inventory", "validate-paper"}:
            result = run_diagnostics(broker, data, store, now)
            if os.getenv("GITHUB_ACTIONS") == "true":
                # Phase 2 inspects notification configuration but sends no messages.
                print(
                    json.dumps(
                        {
                            "mode": "PAPER_ONLY",
                            "status": "DIAGNOSTIC_PERSISTED_PRIVATELY",
                            "run_id": result["run_id"],
                            "execution_ready": False,
                            "broker_mutations": 0,
                        }
                    )
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
        failure = {"status": "FAILED", "reason": redact(code)}
        if args.command in {"inventory", "validate-paper"}:
            print(json.dumps(failure))
        else:
            notify(failure)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
