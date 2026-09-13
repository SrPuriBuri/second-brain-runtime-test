"""Manual research CLI. No trading client or execution package is imported."""

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path

from trading_runtime.config import Config, SafetyError
from trading_runtime.private_store import PrivateRepoStore
from .data import HistoricalSource
from .reports import persist, safety
from .bars import Dataset
from .candidates import grid
from .experiment import (
    select,
    evaluate,
    run_oos,
    save_json,
    implementation_commit,
    implementation_hash,
)
from .splits import sample_days, validate_protocol
from trading_runtime.models import Policy
from trading_runtime.private_store import authority_json


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command",
        choices=["data-audit", "run-candidate", "run-all", "validate", "oos", "report"],
    )
    parser.add_argument("--cache", default=".research-cache")
    parser.add_argument(
        "--dataset",
        help="Local JSON bundle; historical IEX 5Min raw bars and actual calendar",
    )
    parser.add_argument("--protocol", help="Canonical private protocol-v1.json")
    parser.add_argument("--policy", help="Canonical private POLICY.md")
    parser.add_argument("--selection", help="Frozen selection.json written by run-all")
    parser.add_argument(
        "--strategy",
        choices=[
            "opening_momentum",
            "relative_strength",
            "gap_continuation",
            "mean_reversion",
        ],
    )
    parser.add_argument("--output", default="research-output")
    args = parser.parse_args(argv)
    try:
        cloud_run = (
            args.command == "run-all"
            and os.getenv("GITHUB_ACTIONS") == "true"
            and not args.dataset
        )
        if args.dataset or (args.command != "data-audit" and not cloud_run):
            output = Path(args.output)
            if args.command in {"report", "validate"}:
                if not args.selection:
                    raise SafetyError("SELECTION_FILE_REQUIRED")
                result = json.loads(Path(args.selection).read_text(encoding="utf-8"))
                summary = {
                    "decision": result["decision"],
                    "selected_variant": result["selected_variant"],
                    "oos_status": result["oos_status"],
                }
                save_json(
                    output / (args.command + ".json"),
                    summary if args.command == "report" else result["validation"],
                )
            else:
                if not args.dataset:
                    raise SafetyError("HISTORICAL_DATASET_REQUIRED")
                dataset = Dataset(
                    json.loads(Path(args.dataset).read_text(encoding="utf-8"))
                )
                if args.command == "data-audit":
                    save_json(output / "data-audit.json", dataset.audit())
                else:
                    if not args.protocol or not args.policy:
                        raise SafetyError("CANONICAL_RESEARCH_CONFIG_REQUIRED")
                    protocol = json.loads(
                        Path(args.protocol).read_text(encoding="utf-8")
                    )
                    validate_protocol(protocol)
                    policy = Policy.model_validate(
                        authority_json(Path(args.policy).read_text(encoding="utf-8"))
                    )
                    project = Path(args.policy).resolve().parent
                    claim = (
                        project
                        / "data/evidence/research-v1/oos-claims"
                        / ("oos-" + validate_protocol(protocol) + ".json")
                    )
                    if args.command in {"run-all", "run-candidate"} and claim.exists():
                        raise SafetyError("RESEARCH_PROTOCOL_OOS_ALREADY_CONSUMED")
                    if args.command == "run-all":
                        if (output / "selection.json").exists():
                            raise SafetyError("RESEARCH_OUTPUT_EXISTS")
                        result = select(dataset, protocol, policy)
                        save_json(output / "selection.json", result)
                    elif args.command == "run-candidate":
                        if not args.strategy:
                            raise SafetyError("STRATEGY_REQUIRED")
                        if (output / (args.strategy + ".json")).exists():
                            raise SafetyError("RESEARCH_OUTPUT_EXISTS")
                        days = sample_days(dataset, protocol, "development")
                        result = {
                            v.id: evaluate(dataset, days, v, protocol, policy)
                            for v in grid(protocol)
                            if v.family == args.strategy
                        }
                        save_json(
                            output / (args.strategy + ".json"),
                            {
                                "sample": "development",
                                "dataset_hash": dataset.hash,
                                "protocol_hash": validate_protocol(protocol),
                                "strategy_version": protocol["version"],
                                "git_commit": implementation_commit(),
                                "implementation_hash": implementation_hash(),
                                "timestamp": datetime.now(timezone.utc).isoformat(),
                                "data_range": protocol["development"],
                                "source": dataset.metadata,
                                "variants": result,
                            },
                        )
                    elif args.command == "oos":
                        if not args.selection:
                            raise SafetyError("SELECTION_FILE_REQUIRED")
                        selection = json.loads(
                            Path(args.selection).read_text(encoding="utf-8")
                        )
                        project = Path(args.policy).resolve().parent
                        if (
                            project.name != "ai-stock-trader"
                            or project.parent.name != "projects"
                        ):
                            raise SafetyError("CANONICAL_CLAIM_PATH_REQUIRED")
                        result = run_oos(
                            dataset,
                            protocol,
                            policy,
                            selection,
                            project / "data/evidence/research-v1/oos-claims",
                        )
                        save_json(output / "oos.json", result)
            print(
                json.dumps({"status": "LOCAL_RESEARCH_COMPLETE", "broker_mutations": 0})
            )
            return 0
        config = Config.from_env()
        store = PrivateRepoStore(config.pat)
        before = safety(store)
        source = HistoricalSource(config, args.cache)
        if cloud_run:
            from .cloud import run_cloud

            run_cloud(source, store, args.cache)
            print(
                json.dumps(
                    {"status": "RESEARCH_EVIDENCE_PERSISTED", "broker_mutations": 0}
                )
            )
            return 0
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
