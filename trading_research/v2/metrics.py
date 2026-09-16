"""Frozen scalar metrics and C2 descriptive Type-7 quantiles."""

from collections import Counter, defaultdict
from datetime import datetime
import math
from statistics import median

from trading_runtime.config import SafetyError
from .models import NY
from .bindings import PINS
from .provenance import Provenance, require_records, require_development_date


def scored(records):
    require_records(records)
    return sorted((r for r in records if r.get("status") == "SCORED"
                   and not r.get("integrity_violations", 0) and r.get("R") is not None and math.isfinite(r["R"])),
                  key=exit_order)


def exit_order(record):
    return datetime.fromisoformat(record["accounting_exit_timestamp"]), record["symbol"]


def severe_tails(records, *, stage, variant, record_type, source=None):
    """C4: one explicitly identified SEVERE ledger, never pooled scenarios."""
    require_records(records, source=source, stage=stage)
    if any((r.get("scenario"), r.get("stage"), r.get("variant"), r.get("record_type"))
           != ("SEVERE", stage, variant, record_type) for r in records):
        raise SafetyError("D1_TAIL_PARTITION")
    values = [float(r["R"]) for r in scored(records)]
    return {"scored_trade_count": len(values),
            "left_tail_lt_minus_1R": sum(x < -1.0 for x in values),
            "left_tail_lt_minus_2R": sum(x < -2.0 for x in values),
            "right_tail_gt_plus_1R": sum(x > 1.0 for x in values),
            "right_tail_gt_plus_2R": sum(x > 2.0 for x in values)}


def quantiles(values, probabilities):
    values = sorted(float(x) for x in values)
    if any(not math.isfinite(x) for x in values):
        raise SafetyError("D1_NONFINITE")
    result = []
    for p in probabilities:
        if not values:
            result.append(None)
        elif len(values) == 1:
            result.append(values[0])
        else:
            h = (len(values) - 1) * p
            j = math.floor(h)
            g = h - j
            result.append((1 - g) * values[j] + g * values[min(j + 1, len(values) - 1)])
    return result


def core(records):
    trades = scored(records)
    values = [float(r["R"]) for r in trades]
    wins, losses = [x for x in values if x > 0], [x for x in values if x < 0]
    cumulative = peak = drawdown = 0.0
    streak = longest = 0
    for x in values:
        cumulative += x
        peak = max(peak, cumulative)
        drawdown = max(drawdown, peak - cumulative)
        streak = streak + 1 if x < 0 else 0
        longest = max(longest, streak)
    n = len(values)
    return {"trade_count": n, "win_rate": len(wins) / n if n else None,
            "loss_rate": len(losses) / n if n else None,
            "zero_rate": values.count(0) / n if n else None,
            "average_winner_R": sum(wins) / len(wins) if wins else None,
            "average_loser_R": sum(losses) / len(losses) if losses else None,
            "mean_R": sum(values) / n if n else None, "median_R": median(values) if n else None,
            "profit_factor": sum(wins) / abs(sum(losses)) if losses else None,
            "cumulative_R": cumulative, "max_drawdown_R": drawdown,
            "longest_losing_streak": longest,
            "indeterminate": sum(r.get("status", "").startswith("INDETERMINATE") for r in records),
            "integrity_violations": sum(r.get("integrity_violations", 0) for r in records)}


def summarize(records, scheduled_days, spec, *, source=None, stage=None):
    mode = require_records(records, source=source, stage=stage)
    if mode is Provenance.DEVELOPMENT:
        for day in scheduled_days:
            require_development_date(day)
    trades = scored(records)
    result = {**PINS, "source": mode.value, **core(records)}
    if mode is Provenance.DEVELOPMENT:
        result["stage"] = "development"
    if records and records[0].get("fixture_kind") is not None:
        result["fixture_kind"] = records[0]["fixture_kind"]
    result["trades_per_scheduled_day"] = len(trades) / len(scheduled_days) if scheduled_days else None
    result["no_trade_days_by_reason"] = dict(Counter(r.get("reason", r["status"]) for r in records if r["status"] != "SCORED"))
    for name in ("wall", "tradable"):
        key = f"time_in_market_{name}_minutes"
        result[key] = sum(r[key] for r in trades)
    result["R_quantiles_01_05_25_50_75_95_99"] = quantiles([r["R"] for r in trades], spec.clarification(2)["rules"]["descriptive_R_quantiles"]["probabilities"])
    result["daily"] = {d: {"R": 0.0, "count": 0} for d in scheduled_days}
    for r in trades:
        if r["session"] not in result["daily"]:
            raise SafetyError("D1_UNSCHEDULED_RESULT")
        result["daily"][r["session"]]["R"] += r["R"]
        result["daily"][r["session"]]["count"] += 1
    selectors = {
        "symbol": lambda r: r["symbol"], "year": lambda r: r["session"][:4],
        "month": lambda r: r["session"][:7],
        "quarter": lambda r: r["session"][:4] + "Q" + str((int(r["session"][5:7]) - 1) // 3 + 1),
        "entry_time_bucket": lambda r: datetime.fromisoformat(r["entry_timestamp"]).astimezone(NY).strftime("%H:%M"),
        "volatility_regime": lambda r: r["volatility_regime"],
    }
    for group, key in selectors.items():
        buckets = defaultdict(list)
        for r in trades:
            buckets[key(r)].append(r)
        result[group] = {k: core(v) for k, v in sorted(buckets.items())}
        if group in {"symbol", "month"}:
            ranked = sorted(buckets, key=lambda k: (-sum(r["R"] for r in buckets[k]), k))
            best = ranked[0] if ranked else None
            positive = sum(max(0, sum(r["R"] for r in rows)) for rows in buckets.values())
            result[f"positive_{group}_contribution_share"] = max(0, sum(r["R"] for r in buckets[best])) / positive if positive else None
            result[f"exclude_best_{group}"] = core([r for r in trades if key(r) != best])
            result[f"best_{group}"] = best
    best = min(trades, key=lambda r: (-r["R"], *exit_order(r))) if trades else None
    positive = sum(max(0, r["R"]) for r in trades)
    result["positive_trade_contribution_share"] = max(0, best["R"]) / positive if positive else None
    remaining = list(trades)
    if best is not None:
        remaining.remove(best)
    result["exclude_best_trade"] = core(remaining)
    return result


def summarize_ledger(records, scheduled_days, spec, *, stage, variant, scenario, record_type, source=None):
    """An explicit partition is required even for an empty ledger."""
    if any((r.get("stage"), r.get("variant"), r.get("scenario"), r.get("record_type"))
           != (stage, variant, scenario, record_type) for r in records):
        raise SafetyError("D1_METRIC_PARTITION")
    result = summarize(records, scheduled_days, spec, source=source, stage=stage)
    result.update(stage=stage, variant=variant, scenario=scenario, record_type=record_type)
    if scenario == "SEVERE":
        result["tail_counts"] = severe_tails(records, stage=stage, variant=variant, record_type=record_type, source=source)
    return result
