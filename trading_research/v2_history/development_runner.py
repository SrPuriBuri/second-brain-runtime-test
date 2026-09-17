"""Two-phase D2 runner: no core changes, no historical access before pushed freeze."""

import argparse
from datetime import datetime, timezone
import json
import math
from pathlib import Path

from trading_research.v2.bindings import PINS, canonical, digest
from trading_research.v2.bootstrap import lower_bound
from trading_research.v2.gates import evaluate
from trading_research.v2.ledger import Ledger, schedule
from trading_research.v2.metrics import summarize_ledger
from trading_research.v2.provenance import Provenance
from trading_research.v2.simulator import simulate_session, no_trade
from trading_runtime.config import SafetyError
from .adapter import DevelopmentArchive, make_session
from .audit import Access, committed_identity, git, immutable_json, metadata, verify_barrier

SOURCE = Provenance.DEVELOPMENT.value


def serializable(value):
    if isinstance(value, float) and not math.isfinite(value):
        return "-Infinity" if value == -math.inf else "Infinity" if value == math.inf else "NaN"
    if isinstance(value, dict):
        return {k: serializable(v) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [serializable(v) for v in value]
    return value


def trial_value(slot, spec, runtime):
    stage, variant, cost, kind, delay, period = slot
    if stage != "development":
        raise SafetyError("D2_STAGE_FORBIDDEN")
    # D1R hashes purpose verbatim. Bind adapter/runtime through this existing
    # field rather than add ignored fields or change the frozen ledger core.
    purpose = canonical({"phase": "3V2-D2", "D2_adapter_hash": runtime["D2_adapter_hash"],
                         "D2_development_runtime_hash": runtime["D2_development_runtime_hash"],
                         "purpose": "PREREGISTERED_DEVELOPMENT", "slot": slot}).decode()
    return {**PINS, "implementation_hash": runtime["D1R_implementation_hash"],
            "execution_config_hash": runtime["execution_config_hash"],
            "dependency_fingerprint": runtime["dependency_fingerprint"],
            "child_view_id": spec.base["bindings"]["child_view_id"],
            "parent_snapshot_id": spec.base["bindings"]["parent_snapshot_id"],
            "family": spec.base["hypotheses"][0]["id"], "variant": variant,
            "stage": stage, "cost_scenario": cost, "record_type": kind,
            "delay_minutes": delay, "subperiod": period, "purpose": purpose,
            "seed": spec.base["bootstrap"]["seed"] if kind == "BOOTSTRAP" else None}


def decision(gates):
    failed = [r for r in gates["criteria"] if r["status"] == "FAIL"]
    if any("indeterminate" in r["criterion"] or "integrity" in r["criterion"] for r in failed):
        return "V2_DEVELOPMENT_INVALID"
    if not failed:
        return "V2_DEVELOPMENT_PASS"
    # Pure sample/frequency failures only. Compound positivity/robustness
    # conditions are not reinterpreted as sample-only after results.
    sample_only = all(r["criterion"].endswith(("trade_count", ".count"))
                      or r["criterion"].startswith("minimum_symbols_with_at_least_") for r in failed)
    return "V2_DEVELOPMENT_MORE_DATA_REQUIRED" if sample_only else "V2_DEVELOPMENT_NO_GO"


def execute(bars, archive, spec, runtime, out, access):
    slots = [s for s in schedule(spec) if s[0] == "development"]
    if len(slots) != 26:
        raise SafetyError("D2_TRIAL_SCHEDULE")
    ledger = Ledger(out / "DEVELOPMENT_TRIAL_LEDGER_V2", spec, provenance=SOURCE)
    if ledger.read():
        raise SafetyError("D2_EXISTING_ATTEMPT_REQUIRES_EXACT_RECOVERY")
    days = sorted(bars)
    results, bootstraps, subperiods, controls = {}, {}, {}, {}
    completed = []
    unique_exposed = set()
    first_exposure = None
    for slot in slots:
        _, variant, cost, kind, _, period = slot
        value = trial_value(slot, spec, runtime)
        started = ledger.begin(value, source=SOURCE, historical_price_rows_exposed=0)
        attempt_rows = 0
        try:
            if first_exposure is None:
                first_exposure = {"utc": datetime.now(timezone.utc).isoformat(), "trial_id": started["trial_id"],
                                  "session": days[0], "operation": "HistoricalDevelopmentSession then frozen simulate_session/features",
                                  "runtime_hash": runtime["D2_development_runtime_hash"]}
                immutable_json(out / "FIRST_PERFORMANCE_EXPOSURE_V2.json", first_exposure)
            if kind == "BOOTSTRAP":
                daily = results[variant]["STRESS"]["summary"]["daily"]
                bound = lower_bound(daily, "development", spec, source=SOURCE)
                artifact = {"source": SOURCE, "stage": "development", "variant": variant,
                            "lower_bound": bound, "seed": spec.base["bootstrap"]["seed"],
                            "resamples": spec.base["bootstrap"]["resamples"], "scheduled_days": len(days)}
                bootstraps[variant] = artifact
            elif kind == "SUBPERIOD":
                start, end = period.split("-")
                chosen_days = [d for d in days if start <= d[:4] <= end]
                records = [r for r in results[variant]["STRESS"]["records"] if r["session"] in chosen_days]
                artifact = summarize_ledger(records, chosen_days, spec, stage="development", variant=variant,
                                            scenario="STRESS", record_type="STRATEGY", source=SOURCE)
                subperiods.setdefault(variant, {})[period] = artifact
            else:
                records = []
                for day in days:
                    session = make_session(archive.calendar[day], bars[day], archive.mask)
                    identities = {(s, b.start.isoformat()) for s, rows in bars[day].items() for b in rows}
                    unique_exposed.update(identities)
                    count = len(identities)
                    attempt_rows += count
                    access.counters["historical_price_rows_exposed_to_strategy"] += count
                    if kind == "NO_TRADE":
                        record = {**no_trade([session])[0], "variant": variant, "scenario": cost, "record_type": kind}
                    else:
                        record = simulate_session(session, spec, variant, cost,
                                                  delay_diagnostic=kind == "DELAY_DIAGNOSTIC",
                                                  control=kind == "TIME_MATCHED_SPY")
                    access.counters["historical_signals"] += int("signal_timestamp" in record)
                    access.counters["historical_scored_trades"] += int(record["status"] == "SCORED")
                    access.counters["historical_R_calculations"] += int(record["status"] == "SCORED")
                    records.append(record)
                artifact = {"records": records, "summary": summarize_ledger(records, days, spec, stage="development",
                            variant=variant, scenario=cost, record_type=kind, source=SOURCE)}
                if kind == "STRATEGY":
                    results.setdefault(variant, {})[cost] = artifact
                elif kind == "TIME_MATCHED_SPY":
                    controls.setdefault(kind, {})[cost] = artifact
                else:
                    controls[kind] = artifact
            encoded = serializable(artifact)
            immutable_json(out / "outcomes" / (started["trial_id"] + ".json"), encoded)
            ledger.finish(started["trial_id"], started["attempt"], status="PASS", outcome_hash=digest(encoded),
                          historical_price_rows_exposed=attempt_rows)
            completed.append(started["trial_id"])
            print(json.dumps({"completed_registered_records": len(completed), "expected": 26}), flush=True)
        except Exception as error:
            failure = {"exception_type": type(error).__name__, "reason": str(error) if isinstance(error, SafetyError) else "D2_EXECUTION_EXCEPTION",
                       "trial_id": started["trial_id"], "attempt_rows_exposed": attempt_rows}
            immutable_json(out / "outcomes" / (started["trial_id"] + ".failure.json"), failure)
            ledger.finish(started["trial_id"], started["attempt"], status="INTERRUPTED", outcome_hash=digest(failure),
                          historical_price_rows_exposed=attempt_rows)
            raise
    canonical_variant = spec.base["selection"]["canonical_id"]
    bundle = {"source": SOURCE, "variant": canonical_variant,
              "scenarios": {k: v["summary"] for k, v in results[canonical_variant].items()},
              "neighbors": {v: {k: a["summary"] for k, a in cs.items()} for v, cs in results.items() if v != canonical_variant},
              "delay": controls["DELAY_DIAGNOSTIC"]["summary"],
              "controls": {k: v["summary"] for k, v in controls["TIME_MATCHED_SPY"].items()},
              "bootstrap_lower_bound": bootstraps[canonical_variant]["lower_bound"],
              "subperiods": subperiods[canonical_variant]}
    gates = evaluate(bundle, "development", spec)
    for name, value in (("DEVELOPMENT_RESULTS_V2.json", {"variants": results, "subperiods": subperiods}),
                        ("DEVELOPMENT_GATES_V2.json", gates), ("DEVELOPMENT_BOOTSTRAP_V2.json", bootstraps),
                        ("DEVELOPMENT_CONTROLS_V2.json", controls)):
        immutable_json(out / name, serializable(value))
    result = {"decision": decision(gates), "completed_records": len(completed),
              "global_unique_historical_price_rows_exposed_to_strategy": len(unique_exposed),
              "first_performance_exposure": first_exposure, "code_changes_after_first_performance_exposure": False,
              "passed_gates": [r["criterion"] for r in gates["criteria"] if r["status"] == "PASS"],
              "failed_gates": [r for r in gates["criteria"] if r["status"] == "FAIL"], **access.counters,
              "trial_ledger_hash": digest(ledger.read()), "runtime": runtime}
    immutable_json(out / "DEVELOPMENT_DECISION_V2.json", serializable(result))
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("preflight", "run-development"))
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--pre-commit")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    access = Access(args.data_root)
    access.install()
    public = Path(__file__).resolve().parents[2]
    out = args.project / "research-v2/development-v2"
    performance = False
    try:
        spec, parent, items, calendar, mask = metadata(args.project, args.data_root)
        if args.command == "preflight":
            result = {"status": "PASS", "objects_selected": len(items), "partition_plan_hash": digest(items),
                      "development_schedule_hash": digest([s for s in schedule(spec) if s[0] == "development"]),
                      "price_access_counters": access.counters}
            if args.output:
                immutable_json(args.output, result)
            print(json.dumps(result, sort_keys=True))
            return 0
        manifest = json.loads((out / "D2_PRE_DEVELOPMENT_MANIFEST_V2.json").read_bytes())
        runtime = verify_barrier(args.project.resolve(), public, args.pre_commit, manifest)
        if manifest.get("partition_plan_hash") != digest(items):
            raise SafetyError("D2_PARTITION_PLAN_MISMATCH")
        if (out / "FIRST_PERFORMANCE_EXPOSURE_V2.json").exists():
            raise SafetyError("D2_EXISTING_EXPOSURE_REQUIRES_RECOVERY_AUTHORIZATION")
        access.phase_b = True
        archive = DevelopmentArchive(parent, items, calendar, mask, access)
        bars, audit = archive.structural_audit()
        immutable_json(out / "HISTORICAL_ADAPTER_AUDIT_V2.json", audit)
        if committed_identity(public, spec) != runtime or git(public, "status", "--porcelain").strip():
            raise SafetyError("D2_CODE_CHANGED_BEFORE_PERFORMANCE")
        performance = True
        result = execute(bars, archive, spec, runtime, out, access)
        if committed_identity(public, spec) != runtime or git(public, "status", "--porcelain").strip():
            raise SafetyError("D2_CODE_CHANGED_AFTER_PERFORMANCE")
        print(json.dumps({"decision": result["decision"], "completed_records": result["completed_records"]}))
        return 0
    except Exception as error:
        failure = {"decision": "V2_DEVELOPMENT_INVALID",
                   "reason": "D2_EXECUTION_INTEGRITY_FAILURE" if performance else "D2_POST_DATA_PRE_PERFORMANCE_ADAPTER_OR_DATA_FAILURE" if access.phase_b else "D2_PRE_EXPOSURE_BLOCK",
                   "exception_type": type(error).__name__, "detail": str(error) if isinstance(error, SafetyError) else "D2_EXCEPTION",
                   "performance_execution_started": performance, **access.counters}
        if args.command == "run-development":
            immutable_json(out / "D2_FAILURE_V2.json", failure)
        print(json.dumps(failure, sort_keys=True))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
