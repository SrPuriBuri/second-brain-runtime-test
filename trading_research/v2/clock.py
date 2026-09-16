"""Half-open verified halt intervals and C3 accounting metadata."""

from datetime import timedelta, timezone

STEP = timedelta(minutes=5)


def overlap_seconds(start, end, halts):
    """Duration of the union, so overlapping source events are not double-counted."""
    pieces = sorted((max(start, a), min(end, b)) for a, b in halts if a < end and b > start)
    total = 0.0
    cursor = start
    for a, b in pieces:
        a = max(a, cursor)
        if b > a:
            total += (b.astimezone(timezone.utc) - a.astimezone(timezone.utc)).total_seconds()
            cursor = b
    return total


def wall_minutes(start, end):
    return (end.astimezone(timezone.utc) - start.astimezone(timezone.utc)).total_seconds() / 60


def tradable_minutes(start, end, halts):
    return wall_minutes(start, end) - overlap_seconds(start, end, halts) / 60


def halted(at, halts):
    return any(a <= at < b for a, b in halts)


def fully_halted(start, end, halts):
    return overlap_seconds(start, end, halts) == wall_minutes(start, end) * 60


def exit_boundary(entry, close, halts, holding_minutes):
    flat = close - timedelta(minutes=45)
    at = entry
    while at < flat:
        if tradable_minutes(entry, at, halts) >= holding_minutes:
            return at
        at += STEP
    return flat


def timing(entry, start, end, halts, *, exact):
    lower = start if exact else max(entry, start)
    upper = start if exact else end
    wall = wall_minutes(entry, upper)
    return {
        "execution_time_precision": "EXACT" if exact else "BAR_INTERVAL",
        "execution_timestamp": upper.isoformat() if exact else None,
        "execution_time_lower_bound": lower.isoformat(),
        "execution_time_upper_bound": upper.isoformat(),
        "confirmation_timestamp": upper.isoformat(),
        "accounting_exit_timestamp": upper.isoformat(),
        "time_in_market_wall_minutes": wall,
        "time_in_market_tradable_minutes": tradable_minutes(entry, upper, halts),
        "time_in_market_wall_lower_bound": max(0, wall_minutes(entry, lower)),
        "time_in_market_wall_upper_bound": wall,
    }
