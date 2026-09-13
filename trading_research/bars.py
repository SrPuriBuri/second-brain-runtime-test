"""Immutable start-stamped bars. Missing observations are never filled."""

from dataclasses import dataclass
from datetime import timedelta
import math

from trading_runtime.market_calendar import aware, NY, session_from_row
from trading_runtime.config import SafetyError
from .data import digest


@dataclass(frozen=True)
class Bar:
    t: object
    o: float
    h: float
    low: float
    c: float
    v: float

    @property
    def end(self):
        return self.t + timedelta(minutes=5)

    @classmethod
    def parse(cls, row):
        values = [float(row[k]) for k in ("o", "h", "l", "c", "v")]
        o, h, low, c, v = values
        if (
            not all(math.isfinite(x) for x in values)
            or not (0 < low <= min(o, c) <= max(o, c) <= h)
            or v <= 0
        ):
            raise SafetyError("INVALID_HISTORY_BAR")
        t = aware(row["t"])
        if t.second or t.microsecond or t.minute % 5:
            raise SafetyError("BAR_NOT_ON_GRID")
        return cls(t, o, h, low, c, v)


class Dataset:
    def __init__(self, bundle):
        if (
            bundle.get("feed") != "iex"
            or bundle.get("adjustment") != "raw"
            or bundle.get("timeframe") != "5Min"
        ):
            raise SafetyError("UNSUPPORTED_DATA_PROVENANCE")
        self.hash = digest(bundle)
        self.sessions = {r["date"]: session_from_row(r) for r in bundle["calendar"]}
        if len(self.sessions) != len(bundle["calendar"]):
            raise SafetyError("DUPLICATE_SESSION")
        self.days = sorted(self.sessions)
        self.bars = {}
        self.outside_session = 0
        self.invalid_bars = 0
        self.metadata = {
            k: bundle.get(k)
            for k in (
                "source",
                "feed",
                "adjustment",
                "asof",
                "timeframe",
                "retrieved_at",
            )
        }
        for symbol, rows in bundle["bars"].items():
            self.bars[symbol] = {}
            seen = set()
            for row in rows:
                stamp = aware(row["t"])
                if stamp in seen:
                    raise SafetyError("DUPLICATE_BAR")
                seen.add(stamp)
                day = str(stamp.astimezone(NY).date())
                session = self.sessions.get(day)
                if (
                    session is None
                    or stamp < session.open
                    or stamp + timedelta(minutes=5) > session.close
                ):
                    self.outside_session += 1
                    continue
                try:
                    bar = Bar.parse(row)
                except (SafetyError, ValueError, KeyError, TypeError):
                    self.invalid_bars += 1
                    continue
                self.bars[symbol].setdefault(day, {})[bar.t] = bar

    def window(self, symbol, day, end):
        session = self.sessions[day]
        # The only view candidates receive; it cannot contain a future or partial bar.
        slots = int((end - session.open).total_seconds() // 300)
        if slots <= 0 or end > session.close:
            return None
        rows = self.bars.get(symbol, {}).get(day, {})
        required = [session.open + timedelta(minutes=5 * i) for i in range(slots)]
        if any(t not in rows for t in required):
            return None
        return tuple(rows[t] for t in required if rows[t].end <= end)

    def audit(self):
        coverage = {}
        for symbol in self.bars:
            expected, observed, complete = 0, 0, 0
            by_year = {}
            for day in self.days:
                session = self.sessions[day]
                count = len(self.bars[symbol].get(day, {}))
                total = int((session.close - session.open).total_seconds() // 300)
                expected += total
                observed += count
                complete += int(count == total)
                year = by_year.setdefault(day[:4], {"expected": 0, "observed": 0})
                year["expected"] += total
                year["observed"] += count
            coverage[symbol] = {
                "expected_bars": expected,
                "observed_bars": observed,
                "missing_bars": expected - observed,
                "complete_sessions": complete,
                "sessions": len(self.days),
                "by_year": by_year,
            }
        return {
            "dataset_hash": self.hash,
            **self.metadata,
            "first_session": self.days[0] if self.days else None,
            "last_session": self.days[-1] if self.days else None,
            "coverage": coverage,
            "invalid_bars": self.invalid_bars,
            "outside_regular_session": self.outside_session,
            "duplicate_policy": "REJECT_DATASET",
            "missing_policy": "NO_FORWARD_FILL",
            "point_in_time_stock_universe": False,
            "historical_revisions": "AS_RETRIEVED_NOT_ORIGINAL_VINTAGE",
        }
