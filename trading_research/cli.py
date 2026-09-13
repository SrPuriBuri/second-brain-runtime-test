"""Manual research CLI. No trading client or execution package is imported."""

import argparse
from datetime import datetime, timezone
import json
import os

from trading_runtime.config import Config, SafetyError
from trading_runtime.private_store import PrivateRepoStore
from .data import HistoricalSource
from .reports import persist, safety


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["data-audit"])
    parser.add_argument("--cache", default=".research-cache")
    args = parser.parse_args(argv)
    try:
        config = Config.from_env()
        store = PrivateRepoStore(config.pat)
        before = safety(store)
        source = HistoricalSource(config, args.cache)
        report = source.audit_access()
        report.update(
            {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "git_commit": os.getenv("GITHUB_SHA"),
                "github_run_id": os.getenv("GITHUB_RUN_ID"),
                "safety_before": before,
                "safety_after": safety(store),
                "orders_submitted": 0,
                "orders_cancelled": 0,
                "positions_closed": 0,
            }
        )
        name = "data_audit_" + os.environ.get(
            "GITHUB_RUN_ID", datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        )
        persist(store, name, report)
        print(
            json.dumps({"status": "RESEARCH_EVIDENCE_PERSISTED", "broker_mutations": 0})
        )
        return 0
    except Exception as exc:
        print(
            json.dumps(
                {
                    "status": "FAILED",
                    "reason": str(exc)
                    if isinstance(exc, SafetyError)
                    else "RESEARCH_FAILED",
                }
            )
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
