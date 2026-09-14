"""Bar integrity only. This module never computes asset or strategy returns."""

from collections import Counter
from datetime import timedelta, timezone
import math

from trading_runtime.config import SafetyError
from trading_runtime.market_calendar import aware, session_from_row, NY

OOS_START = "2025-01-01"


def pre_oos_interval(start, end):
    # Stricter than the permission: this phase downloads no OOS prices at all.
    from datetime import date

    first = utc_stamp(start).date() if "T" in start else date.fromisoformat(start)
    last = utc_stamp(end).date() if "T" in end else date.fromisoformat(end)
    if first > last or last.isoformat() >= OOS_START:
        raise SafetyError("FOUNDATION_PRICE_WINDOW_FORBIDDEN")


def utc_stamp(value):
    return aware(value).astimezone(timezone.utc)


def audit_bars(bars, calendar, symbols, minutes=5):
    if minutes not in (1, 5):
        raise SafetyError("UNSUPPORTED_AUDIT_INTERVAL")
    sessions = {row["date"]: session_from_row(row) for row in calendar}
    if len(sessions) != len(calendar):
        raise SafetyError("DUPLICATE_CALENDAR_SESSION")
    if sessions:
        pre_oos_interval(min(sessions), max(sessions))
    result = {}
    for symbol in symbols:
        seen, valid, counters = set(), set(), Counter()
        previous = None
        for row in bars.get(symbol, []):
            counters["received"] += 1
            try:
                stamp = utc_stamp(row["t"])
            except (SafetyError, ValueError, TypeError, KeyError):
                counters["invalid_timestamp"] += 1
                continue
            if stamp.date().isoformat() >= OOS_START:
                raise SafetyError("FOUNDATION_OOS_PRICE_ROW_FORBIDDEN")
            if previous and stamp < previous:
                counters["out_of_order"] += 1
            previous = stamp
            if stamp in seen:
                counters["duplicates"] += 1
                continue
            seen.add(stamp)
            day = stamp.astimezone(NY).date().isoformat()
            session = sessions.get(day)
            if not session or not session.open <= stamp < session.close:
                counters["outside_session"] += 1
                continue
            if (stamp - session.open).total_seconds() % (60 * minutes):
                counters["off_grid"] += 1
                continue
            try:
                o, h, low, c, volume = [
                    float(row[k]) for k in ("o", "h", "l", "c", "v")
                ]
                if (
                    not all(math.isfinite(x) for x in (o, h, low, c, volume))
                    or not 0 < low <= min(o, c) <= max(o, c) <= h
                    or volume < 0
                ):
                    raise ValueError()
            except (ValueError, TypeError, KeyError):
                counters["invalid_ohlcv"] += 1
                continue
            if volume == 0:
                counters["zero_volume"] += 1
            valid.add(stamp)
        per_session = {}
        for day, session in sorted(sessions.items()):
            count = int((session.close - session.open).total_seconds() / (60 * minutes))
            slots = {
                session.open + timedelta(minutes=i * minutes) for i in range(count)
            }
            present = len(slots & valid)
            per_session[day] = {
                "expected": count,
                "observed": present,
                "missing": count - present,
                "open_utc": session.open.astimezone(timezone.utc).isoformat(),
                "close_utc": session.close.astimezone(timezone.utc).isoformat(),
                "early_close": count < 390 // minutes,
            }
        expected = sum(v["expected"] for v in per_session.values())
        missing = sum(v["missing"] for v in per_session.values())
        result[symbol] = {
            "counts": dict(counters),
            "expected": expected,
            "observed": expected - missing,
            "missing": missing,
            "completeness": (expected - missing) / expected if expected else None,
            "sessions": per_session,
            "missing_cause": "UNATTRIBUTED_NO_TRADE_HALT_LISTING_OR_PROVIDER_GAP",
            "status": "SAMPLE_PASS"
            if expected
            and not missing
            and not any(
                counters[k]
                for k in (
                    "duplicates",
                    "invalid_timestamp",
                    "invalid_ohlcv",
                    "zero_volume",
                    "off_grid",
                    "out_of_order",
                )
            )
            else "LIMITATIONS",
        }
    return {
        "symbols": result,
        "performance_computed": False,
        "outage_attribution": "NOT_PROVEN_BY_MISSING_BARS_ALONE",
    }


def split_adjustment_check(raw, adjusted, events):
    """Compare adjustment-factor steps to documented split ratios, never returns."""
    checks = []
    for event in events:
        symbol, day = event["symbol"], event.get("ex_date")
        multiplier = event.get("terms", {}).get("share_multiplier")
        if not day or not multiplier:
            continue
        pre_oos_interval(day, day)
        prices = {utc_stamp(r["t"]): float(r["c"]) for r in adjusted.get(symbol, [])}
        before, after = [], []
        for row in raw.get(symbol, []):
            stamp = utc_stamp(row["t"])
            if stamp.date().isoformat() >= OOS_START:
                raise SafetyError("FOUNDATION_OOS_PRICE_ROW_FORBIDDEN")
            if stamp not in prices or prices[stamp] <= 0:
                continue
            factor = float(row["c"]) / prices[stamp]
            (before if stamp.astimezone(NY).date().isoformat() < day else after).append(
                (stamp, factor)
            )
        if before and after:
            observed = max(before)[1] / min(after)[1]
            checks.append(
                {
                    "symbol": symbol,
                    "ex_date": day,
                    "source_record_id": event["source_record_id"],
                    "documented_share_multiplier": float(multiplier),
                    "observed_factor_step": observed,
                    "status": "MATCH"
                    if math.isclose(observed, float(multiplier), rel_tol=0.005)
                    else "MISMATCH",
                }
            )
    return {
        "checks": checks,
        "status": "CHECKED" if checks else "NO_MATCHED_EVENT_WINDOW",
        "performance_computed": False,
    }
