"""Local data-only archive commands; deliberately separate from the V1 CLI."""

import argparse
import json
from pathlib import Path
import subprocess

from trading_runtime.config import Config, SafetyError
from trading_runtime.private_store import authority_json
from .archive import Archive, atomic_json, data_root, make_plan, require_retention
from .coverage import audit_archive
from .downloader import Downloader, offline_references
from .providers.alpaca import AlpacaFoundationProvider
from .restore import verify_snapshot

COMMANDS = (
    "dataset-plan",
    "dataset-download",
    "dataset-resume",
    "dataset-audit",
    "dataset-freeze",
    "dataset-restore-test",
    "dataset-status",
)


def local_safety(project):
    strategy = authority_json((project / "STRATEGY.md").read_text(encoding="utf-8"))
    kill = json.loads((project / "state/kill-switch.json").read_text())
    readiness = json.loads((project / "state/readiness.json").read_text())
    if (
        strategy["tradable"] is not False
        or kill["enabled"] is not True
        or readiness["execution_enabled"] is not False
        or readiness["execution_ready"] is not False
    ):
        raise SafetyError("ARCHIVE_SAFETY_INVALID")


def main(argv=None, *, config=None, client=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=COMMANDS)
    parser.add_argument("--plan", type=Path)
    parser.add_argument(
        "--retention",
        type=Path,
        help="Provider permission or recorded explicit private-research authorization",
    )
    parser.add_argument(
        "--identity", type=Path, help="Per-symbol issuer/exchange identity evidence"
    )
    parser.add_argument("--snapshot", type=Path)
    parser.add_argument("--max-chunks", type=int, default=10)
    parser.add_argument(
        "--project",
        type=Path,
        default=Path(__file__).resolve().parents[2]
        / "second-brain/projects/ai-stock-trader",
    )
    args = parser.parse_args(argv)
    try:
        local_safety(args.project)
        if args.command == "dataset-restore-test":
            if not args.snapshot:
                raise SafetyError("SNAPSHOT_REQUIRED")
            result = verify_snapshot(args.snapshot)
        else:
            root = data_root()
            print(json.dumps({"AIST_DATA_ROOT": str(root)}))
            plan = json.loads(args.plan.read_text()) if args.plan else make_plan()
            archive = Archive(root, plan)
            if args.command in {"dataset-plan", "dataset-status"}:
                result = archive.status()
                result["plan_path"] = str(archive.path / "plan.json")
            elif args.command in {"dataset-download", "dataset-resume"}:
                retention = (
                    json.loads(args.retention.read_text()) if args.retention else {}
                )
                require_retention(retention)
                config = config or Config.from_env()
                provider = AlpacaFoundationProvider(
                    config, archive.cache, client=client, request_budget=20000
                )
                result = Downloader(archive, provider, retention).run(args.max_chunks)
            else:
                identity = (
                    json.loads(args.identity.read_text()) if args.identity else {}
                )
                report = audit_archive(archive, identity)
                atomic_json(archive.path / "quality.json", report)
                if args.command == "dataset-audit":
                    result = {
                        "accepted": report["accepted"],
                        "report_path": str(archive.path / "quality.json"),
                    }
                else:
                    calendar, actions, _ = offline_references(archive)
                    universe = {
                        "symbols": plan["selection"]["symbols"],
                        "identity_evidence": identity,
                        "selection": "fixed_user_core_no_returns",
                    }
                    sha = (
                        subprocess.check_output(
                            [
                                "git",
                                "-c",
                                "safe.directory=*",
                                "-C",
                                str(Path(__file__).resolve().parents[1]),
                                "rev-parse",
                                "HEAD",
                            ]
                        )
                        .decode()
                        .strip()
                    )
                    snapshot = archive.freeze(universe, calendar, actions, report, sha)
                    result = {
                        "snapshot": str(snapshot),
                        "snapshot_id": snapshot.name,
                        "status": "FROZEN_PENDING_FRESH_PROCESS_RESTORE",
                    }
        local_safety(args.project)
        print(json.dumps(result))
        return 0
    except Exception as exc:
        print(
            json.dumps(
                {
                    "status": "FAILED",
                    "reason": str(exc)
                    if isinstance(exc, SafetyError)
                    else "ARCHIVE_OPERATION_FAILED",
                }
            )
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
