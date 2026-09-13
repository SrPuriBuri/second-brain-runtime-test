"""R distributions and concentration, explicitly distinguishing unknown outcomes."""

from collections import defaultdict
import random
from statistics import mean, median


def quantile(values, q):
    if not values:
        return None
    rows = sorted(values)
    where = (len(rows) - 1) * q
    low = int(where)
    return rows[low] + (rows[min(low + 1, len(rows) - 1)] - rows[low]) * (where - low)


def core(trades):
    # Sort by realized event, never by signal sequence, for equity/drawdown/streak.
    ordered = sorted(trades, key=lambda t: (t["exit_at"], t["symbol"], t["entry_at"]))
    values = [t["r"] for t in ordered if t["r"] is not None]
    wins, losses = [r for r in values if r > 0], [r for r in values if r < 0]
    peak, equity, drawdown, streak, longest = 0.0, 0.0, 0.0, 0, 0
    for value in values:
        equity += value
        peak = max(peak, equity)
        drawdown = max(drawdown, peak - equity)
        streak = streak + 1 if value < 0 else 0
        longest = max(longest, streak)
    return {
        "trade_count": len(trades),
        "scored_trades": len(values),
        "indeterminate": len(trades) - len(values),
        "win_rate": len(wins) / len(values) if values else None,
        "loss_rate": len(losses) / len(values) if values else None,
        "average_winner_r": mean(wins) if wins else None,
        "average_loser_r": mean(losses) if losses else None,
        "expectancy_r": mean(values) if values else None,
        "median_r": median(values) if values else None,
        "profit_factor": sum(wins) / -sum(losses) if losses else None,
        "cumulative_r": sum(values),
        "max_drawdown_r": drawdown,
        "longest_losing_streak": longest,
        "r_quantiles": {
            str(q): quantile(values, q) for q in (0, 0.05, 0.25, 0.5, 0.75, 0.95, 1)
        },
    }


def breakdown(trades, key):
    groups = defaultdict(list)
    for trade in trades:
        groups[key(trade)].append(trade)
    return {name: core(rows) for name, rows in sorted(groups.items())}


def summarize(simulation):
    trades = simulation["trades"]
    result = core(trades)
    days = simulation["sessions"]
    position_minutes = sum(t["minutes"] for t in trades)
    result.update(
        {
            "sessions": days,
            "average_trades_per_day": len(trades) / days if days else None,
            "no_trade_days_pct": 100 * (1 - len({t["day"] for t in trades}) / days)
            if days
            else None,
            "position_minutes": position_minutes,
            "position_minutes_per_session_minute": position_minutes
            / simulation["session_minutes"]
            if simulation["session_minutes"]
            else None,
            "rejections": simulation["rejections"],
        }
    )
    for name, key in {
        "year": lambda t: t["day"][:4],
        "month": lambda t: t["day"][:7],
        "symbol": lambda t: t["symbol"],
        "regime": lambda t: t["regime"],
        "entry_bucket": lambda t: t["entry_bucket"],
    }.items():
        groups = breakdown(trades, key)
        result["by_" + name] = groups
        if name in {"year", "month", "symbol"}:
            best = (
                max(groups, key=lambda g: groups[g]["cumulative_r"]) if groups else None
            )
            positive = sum(max(0, g["cumulative_r"]) for g in groups.values())
            result["top_" + name + "_positive_share"] = (
                max(0, groups[best]["cumulative_r"]) / positive if positive else None
            )
            result["excluding_top_" + name] = core(
                [t for t in trades if key(t) != best]
            )
    scored = [t for t in trades if t["r"] is not None]
    best = max(scored, key=lambda t: t["r"]) if scored else None
    result["excluding_top_trade"] = core([t for t in trades if t is not best])
    result["sector_assessment"] = (
        "Two overlapping broad equity ETFs; not independent sector diversification"
    )
    return result


def bootstrap_lower(trades, days, samples=2000, seed=731):
    groups = {day: [] for day in days}
    for t in trades:
        if t["r"] is not None:
            groups[t["day"]].append(t["r"])
    if not days or not any(groups.values()):
        return None
    rng = random.Random(seed)
    means = []
    for _ in range(samples):
        draws = [groups[rng.choice(days)] for _ in days]
        count = sum(len(d) for d in draws)
        means.append(sum(sum(d) for d in draws) / count if count else 0)
    return quantile(means, 0.05)


def performance_gates(base, stress, protocol, sample):
    g = protocol["gates"]
    reasons = []
    for condition, reason in [
        (base["trade_count"] < g[sample + "_trades"], "INSUFFICIENT_TRADES"),
        (
            base["indeterminate"] > 0 or stress["indeterminate"] > 0,
            "INDETERMINATE_OUTCOMES",
        ),
        (
            base["expectancy_r"] is None or base["expectancy_r"] < g["expectancy_r"],
            "EXPECTANCY",
        ),
        (
            base["profit_factor"] is None or base["profit_factor"] < g["profit_factor"],
            "PROFIT_FACTOR",
        ),
        (
            stress["expectancy_r"] is None or stress["expectancy_r"] <= 0,
            "STRESS_EXPECTANCY",
        ),
        (
            sample != "development"
            and (
                stress["profit_factor"] is None
                or stress["profit_factor"] < g["stress_profit_factor"]
            ),
            "STRESS_PROFIT_FACTOR",
        ),
        (base["max_drawdown_r"] > g["max_drawdown_r"], "DRAWDOWN"),
        (base["longest_losing_streak"] > g["max_losing_streak"], "LOSING_STREAK"),
    ]:
        if condition:
            reasons.append(reason)
    return reasons


def concentration_gates(metrics, protocol, include_year=False):
    reasons = []
    for group in ("symbol", "month"):
        share = metrics["top_" + group + "_positive_share"]
        if share is None or share > protocol["gates"][group + "_positive_share"]:
            reasons.append("CONCENTRATION_" + group.upper())
    for group in ("symbol", "month", "trade") + (("year",) if include_year else ()):
        value = metrics["excluding_top_" + group]["expectancy_r"]
        if value is None or value <= 0:
            reasons.append("EXCLUDING_TOP_" + group.upper())
    return reasons
