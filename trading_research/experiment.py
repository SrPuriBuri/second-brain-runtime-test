"""Preregistered search, sealed selection, and at most one OOS evaluation."""

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import tempfile

from trading_runtime.config import SafetyError
from .candidates import grid, Variant
from .costs import Costs
from .data import digest
from .metrics import summarize, performance_gates, concentration_gates, bootstrap_lower
from .simulator import simulate
from .splits import validate_protocol, sample_days, claim_local_oos
from .walkforward import walkforward


def implementation_commit():
    try:
        return subprocess.check_output(
            ["git", "-c", "safe.directory=*", "rev-parse", "HEAD"],
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        return "UNCOMMITTED_OR_UNAVAILABLE"


def implementation_hash():
    root = Path(__file__).resolve().parent
    return digest(
        {
            p.relative_to(root).as_posix(): p.read_text(encoding="utf-8")
            for p in sorted(root.rglob("*.py"))
        }
    )


def evaluate(dataset, days, variant, protocol, policy):
    return {
        name: summarize(
            simulate(dataset, days, variant, protocol, policy, Costs(**assumptions))
        )
        for name, assumptions in protocol["costs"].items()
    }


def select(dataset, protocol, policy):
    phash = validate_protocol(protocol)
    variants = grid(protocol)
    development = sample_days(dataset, protocol, "development")
    validation = sample_days(dataset, protocol, "validation")
    dev = {v.id: evaluate(dataset, development, v, protocol, policy) for v in variants}
    candidates, reasons, val = [], {}, {}
    neighbors = {}
    for variant in variants:
        gates = performance_gates(
            dev[variant.id]["baseline"],
            dev[variant.id]["stress"],
            protocol,
            "development",
        )
        nearby = [
            v
            for v in variants
            if v.family == variant.family
            and v.regime == variant.regime
            and v.activity == variant.activity
        ]
        stable = sum(
            dev[v.id]["stress"]["trade_count"] >= protocol["gates"]["neighbor_trades"]
            and (dev[v.id]["stress"]["expectancy_r"] or 0) > 0
            for v in nearby
        )
        neighbors[variant.id] = {
            "positive_stress_neighbors": stable,
            "neighbors": [v.id for v in nearby],
        }
        if stable < protocol["gates"]["positive_neighbors"]:
            gates.append("PARAMETER_FRAGILITY")
        reasons[variant.id] = gates
    # Pick per family on development only. Validation never picks another threshold.
    family_winners = []
    for family in sorted(protocol["thresholds"]):
        qualified = [v for v in variants if v.family == family and not reasons[v.id]]
        qualified.sort(
            key=lambda v: (
                -dev[v.id]["stress"]["expectancy_r"],
                v.regime + v.activity,
                v.id,
            )
        )
        if qualified:
            family_winners.append(qualified[0])
    wf = walkforward(dataset, variants, protocol, policy)
    for winner in family_winners:
        scores = evaluate(dataset, validation, winner, protocol, policy)
        delayed = summarize(
            simulate(
                dataset,
                validation,
                winner,
                protocol,
                policy,
                Costs(**protocol["costs"]["stress"]),
                delay=10,
            )
        )
        combined = summarize(
            simulate(
                dataset,
                development + validation,
                winner,
                protocol,
                policy,
                Costs(**protocol["costs"]["baseline"]),
            )
        )
        gates = performance_gates(
            scores["baseline"], scores["stress"], protocol, "validation"
        )
        gates += concentration_gates(scores["baseline"], protocol)
        gates += [
            r
            for r in concentration_gates(combined, protocol, include_year=True)
            if r == "EXCLUDING_TOP_YEAR"
        ]
        if (
            not delayed["expectancy_r"]
            or delayed["expectancy_r"] <= 0
            or delayed["indeterminate"]
        ):
            gates.append("DELAY_SENSITIVITY")
        if not all(f["passed"] for f in wf):
            gates.append("WALKFORWARD_FAILED")
        if not protocol.get("corporate_actions_verified"):
            gates.append("CORPORATE_ACTIONS_UNVERIFIED")
        if dataset.metadata.get("source") != "alpaca":
            gates.append("NON_OBSERVED_DATA")
        scores.update(
            {
                "delay_10min_stress": delayed,
                "combined_year_exclusion": combined["excluding_top_year"],
                "rejection_reasons": gates,
            }
        )
        val[winner.id] = scores
        if not gates:
            candidates.append(winner)
    candidates.sort(
        key=lambda v: (
            -min(
                dev[v.id]["stress"]["expectancy_r"], val[v.id]["stress"]["expectancy_r"]
            ),
            v.regime + v.activity,
            v.id,
        )
    )
    selected = candidates[0] if candidates else None
    benchmarks = {
        "no_trade": {"cumulative_r": 0},
        "simple_momentum": evaluate(
            dataset, development, Variant("baseline", 0, False, False), protocol, policy
        ),
    }
    spy_returns = []
    for day in development:
        rows = dataset.window("SPY", day, dataset.sessions[day].close)
        if rows:
            c = Costs(**protocol["costs"]["baseline"])
            spy_returns.append(c.exit(rows[-1].c) / c.entry(rows[0].o) - 1)
    benchmarks["spy_regular_session"] = {
        "observations": len(spy_returns),
        "mean_return": sum(spy_returns) / len(spy_returns) if spy_returns else None,
        "unit": "fractional return; not comparable to R",
        "missing_session_policy": "omitted benchmark observations counted; never a candidate selection feature",
    }
    return {
        "protocol_hash": phash,
        "strategy_version": protocol["version"],
        "policy_hash": digest(policy.model_dump(mode="json")),
        "implementation_hash": implementation_hash(),
        "data_source": dataset.metadata,
        "data_ranges": {k: protocol[k] for k in ("development", "validation", "oos")},
        "dataset_hash": dataset.hash,
        "implementation_commit": implementation_commit(),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "selected_variant": selected.id if selected else None,
        "decision": "AWAITING_OOS" if selected else "NO_GO",
        "oos_status": "SEALED",
        "trial_count": {
            "families": 4,
            "parameter_filter_variants": len(variants),
            "development_cost_evaluations": len(variants) * len(protocol["costs"]),
            "validation_family_winners": len(family_winners),
            "validation_cost_evaluations": len(family_winners) * 3,
            "validation_delay_evaluations": len(family_winners),
            "combined_concentration_evaluations": len(family_winners),
            "walkforward_training_evaluations": 2 * len(variants) * 2,
            "walkforward_test_evaluations": sum(
                f["selected_variant"] is not None for f in wf
            ),
            "universes": 1,
            "decision_offsets": protocol["decision_offsets"],
            "benchmarks": 3,
        },
        "development": dev,
        "development_rejections": reasons,
        "parameter_neighbors": neighbors,
        "validation": val,
        "walkforward": wf,
        "benchmarks": benchmarks,
        "data_audit": dataset.audit(),
    }


def run_oos(dataset, protocol, policy, selection, claim_directory, remote_claim=None):
    if selection.get("implementation_commit") != implementation_commit():
        raise SafetyError("OOS_IMPLEMENTATION_CHANGED")
    if selection.get("implementation_hash") != implementation_hash():
        raise SafetyError("OOS_IMPLEMENTATION_CHANGED")
    if selection.get("policy_hash") != digest(policy.model_dump(mode="json")):
        raise SafetyError("OOS_POLICY_CHANGED")
    winner = next(
        (v for v in grid(protocol) if v.id == selection.get("selected_variant")), None
    )
    if winner is None:
        raise SafetyError("NO_SELECTED_OOS_CANDIDATE")
    if (
        selection.get("decision") != "AWAITING_OOS"
        or selection.get("validation", {}).get(winner.id, {}).get("rejection_reasons")
        != []
    ):
        raise SafetyError("OOS_SELECTION_NOT_ACCEPTED")
    # The local marker is consumed even if the remote claim/network subsequently fails.
    claim_local_oos(claim_directory, selection, protocol, dataset.hash)
    if remote_claim:
        remote_claim(
            {
                "protocol_hash": digest(protocol),
                "dataset_hash": dataset.hash,
                "selected_variant": winner.id,
                "implementation_commit": implementation_commit(),
            }
        )
    days = sample_days(dataset, protocol, "oos", oos_authorized=True)
    simulations = {
        name: simulate(dataset, days, winner, protocol, policy, Costs(**assumptions))
        for name, assumptions in protocol["costs"].items()
    }
    scores = {name: summarize(sim) for name, sim in simulations.items()}
    base_sim = simulations["baseline"]
    scores["bootstrap_lower_95"] = bootstrap_lower(
        base_sim["trades"],
        days,
        protocol["bootstrap_resamples"],
        protocol["bootstrap_seed"],
    )
    delayed = summarize(
        simulate(
            dataset,
            days,
            winner,
            protocol,
            policy,
            Costs(**protocol["costs"]["stress"]),
            delay=10,
        )
    )
    scores["delay_10min_stress"] = delayed
    gates = performance_gates(scores["baseline"], scores["stress"], protocol, "oos")
    gates += concentration_gates(scores["baseline"], protocol, include_year=True)
    if len(scores["baseline"]["by_month"]) < protocol["gates"]["oos_months"]:
        gates.append("OOS_MONTH_COUNT")
    if scores["bootstrap_lower_95"] is None or scores["bootstrap_lower_95"] <= 0:
        gates.append("OOS_BOOTSTRAP_UNCERTAINTY")
    if (
        not delayed["expectancy_r"]
        or delayed["expectancy_r"] <= 0
        or delayed["indeterminate"]
    ):
        gates.append("OOS_DELAY_SENSITIVITY")
    if not protocol.get("corporate_actions_verified"):
        gates.append("CORPORATE_ACTIONS_UNVERIFIED")
    return {
        "selected_variant": winner.id,
        "scores": scores,
        "rejection_reasons": gates,
        "decision": "NO_GO" if gates else "PAPER_CANDIDATE",
        "tradable": False,
        "oos_status": "CONSUMED",
        "implementation_commit": implementation_commit(),
        "implementation_hash": implementation_hash(),
        "policy_hash": digest(policy.model_dump(mode="json")),
        "strategy_version": protocol["version"],
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "data_range": protocol["oos"],
        "data_source": dataset.metadata,
        "protocol_hash": digest(protocol),
        "dataset_hash": dataset.hash,
    }


def save_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(value, sort_keys=True, indent=2, allow_nan=False)
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=path.parent, suffix=".partial", delete=False
    ) as handle:
        temporary = Path(handle.name)
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
