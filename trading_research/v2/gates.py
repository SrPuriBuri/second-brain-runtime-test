"""Frozen gates on synthetic summaries. This module does not open any stage."""

import math
import re

from trading_runtime.config import SafetyError
from .bindings import PINS

SOURCE = "SYNTHETIC_GOLDEN_D1"


def check(name, observed, operator, threshold, rule):
    finite = isinstance(observed, (int, float)) and math.isfinite(observed)
    passed = finite and {">": lambda: observed > threshold,
                         ">=": lambda: observed >= threshold,
                         "<=": lambda: observed <= threshold,
                         "==": lambda: observed == threshold}[operator]()
    return {"criterion": name, "status": "PASS" if passed else "FAIL",
            "reason": "SATISFIED" if passed else "UNDEFINED_OR_OUTSIDE_FROZEN_RULE",
            "observed": observed if finite else None, "operator": operator,
            "threshold": threshold if math.isfinite(threshold) else None, "rule": rule}


def neighbor_rules(spec):
    text = spec.base["acceptance"]["development"]["neighboring_variants"]
    # Numbers come from hash-verified authority, including its textual clauses.
    count = int(re.search(r">=(\d+) trades", text)[1])
    pf = float(re.search(r"PF>=(\d+\.\d+)", text)[1])
    dd = float(re.search(r"DD<=(\d+)R", text)[1])
    return count, pf, dd, text


def neighbor_checks(summaries, spec):
    count, pf, dd, rule = neighbor_rules(spec)
    results = []
    for variant in spec.base["hypotheses"][0]["variants"]:
        if variant["role"] != "neighbor":
            continue
        name = variant["id"]
        scenarios = summaries.get(name, {})
        for scenario in ("BASELINE", "STRESS"):
            m = scenarios.get(scenario, {})
            for metric, op, threshold in (("trade_count", ">=", count),
                                          ("mean_R", ">", 0),
                                          ("max_drawdown_R", "<=", dd)):
                results.append(check(f"{name}.{scenario}.{metric}", m.get(metric), op, threshold, rule))
            if scenario == "STRESS":
                results.append(check(f"{name}.STRESS.profit_factor", m.get("profit_factor"), ">=", pf, rule))
        for scenario in spec.base["costs"]:
            m = scenarios.get(scenario, {})
            for metric in ("indeterminate", "integrity_violations"):
                results.append(check(f"{name}.{scenario}.{metric}", m.get(metric), "==", 0, rule))
    return results


def evaluate(bundle, stage, spec):
    """One synthetic stage bundle; absent evidence fails closed.

    This report is not authorization to execute a historical stage. Tail counts
    have no numeric acceptance threshold; their required presence is checked.
    """
    if bundle.get("source") != SOURCE:
        raise SafetyError("D1_REAL_DATA_FORBIDDEN")
    if stage not in spec.base["acceptance"]:
        raise SafetyError("D1_STAGE")
    if bundle.get("variant") != spec.base["selection"]["canonical_id"]:
        raise SafetyError("D1_CANONICAL_ONLY")
    a = spec.base["acceptance"][stage]
    scenarios = bundle.get("scenarios", {})
    stress = scenarios.get("STRESS", {})
    results = []

    def add(name, value, op, threshold, field):
        results.append(check(name, value, op, threshold, str(a[field])))

    for scenario in ("BASELINE", "STRESS"):
        m = scenarios.get(scenario, {})
        for metric, field, op in (
            ("trade_count", "minimum_trades_each_baseline_stress", ">="),
            ("max_drawdown_R", "max_drawdown_R_each_baseline_stress", "<="),
            ("longest_losing_streak", "longest_losing_streak_max", "<="),
            ("mean_R", f"{scenario.lower()}_mean_R_min", ">="),
            ("profit_factor", f"{scenario.lower()}_PF_min", ">="),
        ):
            add(f"{scenario}.{metric}", m.get(metric), op, a[field], field)

    # Retain integrity/indeterminate failures on every evaluated control path.
    all_paths = {**scenarios, "DELAY_STRESS": bundle.get("delay", {}),
                 **{f"SPY_{k}": v for k, v in bundle.get("controls", {}).items()}}
    for scenario in spec.base["costs"]:
        all_paths.setdefault(scenario, {})
        all_paths.setdefault(f"SPY_{scenario}", {})
    for name, m in all_paths.items():
        for metric, field in (("indeterminate", "indeterminate_max_all_scenarios"),
                              ("integrity_violations", "integrity_or_mask_violations_max")):
            add(f"{name}.{metric}", m.get(metric), "<=", a[field], field)

    add("bootstrap_lower_bound", bundle.get("bootstrap_lower_bound"), ">", 0,
        "stress_bootstrap_lower_bound_strictly_positive")
    for group in ("symbol", "month", "trade"):
        field = f"positive_{group}_contribution_share_max"
        add(field, stress.get(f"positive_{group}_contribution_share"), "<=", a[field], field)
        field = "stress_expectancy_after_removing_each_best_symbol_month_trade"
        add(f"exclude_best_{group}", stress.get(f"exclude_best_{group}", {}).get("mean_R"), ">", 0, field)
    field = "minimum_symbols_with_at_least_10_stress_trades"
    number = sum(v.get("trade_count", 0) >= 10 for v in stress.get("symbol", {}).values())
    add(field, number, ">=", a[field], field)
    delay = bundle.get("delay", {})
    field = "delay_10min_stress_expectancy"
    fraction = float(re.search(r">=(\d+)%", a[field])[1]) / 100
    add("delay.mean_R", delay.get("mean_R"), ">", 0, field)
    baseline_count = stress.get("trade_count")
    add("delay.trade_count", delay.get("trade_count"), ">=",
        fraction * baseline_count if baseline_count is not None else math.inf, field)

    spy = bundle.get("controls", {}).get("STRESS", {}).get("daily", {})
    daily = stress.get("daily", {})
    paired = None
    if daily and daily.keys() == spy.keys():
        paired = sum(daily[d]["R"] - spy[d]["R"] for d in sorted(daily)) / len(daily)
    add("paired_control", paired, ">", 0, "paired_control")

    severe = scenarios.get("SEVERE", {})
    required = set(spec.clarification(4)["rules"]["required_integer_counts"])
    tails = severe.get("tail_counts", {})
    present = {"mean_R", "profit_factor", "max_drawdown_R"} <= severe.keys()
    present = present and required <= tails.keys() and all(type(tails[k]) is int and tails[k] >= 0 for k in required)
    add("SEVERE.required_diagnostics_present", int(present), "==", 1, "severe")

    if "years" in a:
        year_rule = a["years"]
        years = list(range(int(spec.base["splits"][stage][0][:4]), int(spec.base["splits"][stage][1][:4]) + 1))
        minimum = int(re.search(r">=(\d+)", year_rule)[1])
        positive = 0
        for year in years:
            m = stress.get("year", {}).get(str(year), {})
            add(f"year.{year}.count", m.get("trade_count"), ">=", minimum, "years")
            value = m.get("mean_R")
            positive += value is not None and math.isfinite(value) and value > 0
        required_positive = int(re.search(r"at least (\d+)/", year_rule)[1]) if stage == "development" else len(years)
        add("positive_years", positive, ">=", required_positive, "years")
    if "quarters" in a:
        minimum = int(re.search(r">=(\d+) STRESS", a["quarters"])[1])
        positive = 0
        year = spec.base["splits"][stage][0][:4]
        for quarter in range(1, 5):
            m = stress.get("quarter", {}).get(f"{year}Q{quarter}", {})
            add(f"quarter.{quarter}.count", m.get("trade_count"), ">=", minimum, "quarters")
            positive += m.get("mean_R") is not None and m["mean_R"] > 0
        add("positive_quarters", positive, ">=", int(re.search(r">=(\d+) quarters", a["quarters"])[1]), "quarters")
    if stage == "development":
        results.extend(neighbor_checks(bundle.get("neighbors", {}), spec))
        for period in re.findall(r"\d{4}-\d{4}", a["subperiods"]):
            add(f"subperiod.{period}", bundle.get("subperiods", {}).get(period, {}).get("mean_R"), ">", 0, "subperiods")
        minimum = int(re.search(r">=(\d+) STRESS", a["regime"])[1])
        bins = stress.get("volatility_regime", {})
        passing = sum(bins.get(k, {}).get("trade_count", 0) >= minimum and
                      bins[k].get("mean_R") is not None and bins[k]["mean_R"] > 0
                      for k in ("LOW", "NORMAL", "HIGH"))
        add("positive_eligible_regimes", passing, ">=", 2, "regime")
    return {**PINS, "source": SOURCE, "stage": stage, "variant": bundle["variant"],
            "status": "PASS" if all(r["status"] == "PASS" for r in results) else "FAIL",
            "criteria": results, "opens_next_stage": False}


def eligible_for_next_stage(variant, completed, spec):
    """Pure gatekeeping predicate, never execution/authorization."""
    stages = ("development", "validation", "internal_holdout", "external_oos")
    if variant != spec.base["selection"]["canonical_id"] or not completed:
        return False
    if tuple(completed) != stages[:len(completed)] or len(completed) > len(stages):
        return False
    return all(r.get("status") == "PASS" and r.get("variant") == variant for r in completed.values())
