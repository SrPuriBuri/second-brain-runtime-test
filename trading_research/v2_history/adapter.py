"""Read-only, metadata-selected monthly objects; no strategy algorithms."""

from collections import Counter
from datetime import date, datetime, timedelta
import gzip
import hashlib
import json

from trading_research.v2.bindings import digest
from trading_research.v2.clock import STEP, fully_halted
from trading_research.v2.models import Bar, HistoricalDevelopmentSession, NY
from trading_research.v2.provenance import require_development_date
from trading_research.v2_protocol import UNIVERSE
from trading_runtime.config import SafetyError


def boundary(start, end):
    a, b = require_development_date(start), require_development_date(end)
    if a > b:
        raise SafetyError("D2_DATE_RANGE")
    return a, b


def calendar_session(row):
    day = row["date"]
    def stamp(value):
        value = datetime.fromisoformat(day + "T" + value) if "T" not in value else datetime.fromisoformat(value)
        return value.replace(tzinfo=NY) if value.tzinfo is None else value.astimezone(NY)
    opened, closed = stamp(row["open"]), stamp(row["close"])
    if opened.date().isoformat() != day or closed.date() != opened.date() or opened >= closed:
        raise SafetyError("D2_CALENDAR_INVALID")
    return opened, closed


def partitions(plan, checksums, checkpoint, calendar, universe, start, end):
    """Prove canonical REGULAR monthly objects contain only authorized sessions."""
    first, last = boundary(start, end)
    if tuple(universe) != tuple(UNIVERSE) or plan["selection"].get("chunking") != "symbol/month":
        raise SafetyError("D2_UNIVERSE_OR_PARTITION")
    selection = plan["selection"]
    if (selection.get("session_scope"), selection.get("feed"), selection.get("timeframe"), selection.get("adjustment_modes")) != ("REGULAR", "sip", "5Min", ["raw"]):
        raise SafetyError("D2_SOURCE_FORMAT")
    days = [date.fromisoformat(r["date"]) for r in calendar]
    if len(days) != len(set(days)) or days != sorted(days):
        raise SafetyError("D2_CALENDAR_ORDER")
    chosen = []
    month = date(first.year, first.month, 1)
    while month <= last:
        following = date(month.year + (month.month == 12), month.month % 12 + 1, 1)
        month_end = following - timedelta(days=1)
        sessions = [d for d in days if month <= d <= month_end]
        # The immutable normalized layer stores regular-session rows only. The
        # checksum-bound calendar proves January 2016 begins on January 4.
        if not sessions or any(not first <= d <= last for d in sessions):
            raise SafetyError("D2_ARCHIVE_PARTITION_INSUFFICIENT")
        for symbol in universe:
            params = dict(symbols=symbol, start=str(month)+"T00:00:00Z",
                          end=str(month_end)+"T23:59:59Z", feed="sip", timeframe="5Min",
                          adjustment="raw", asof="-", sort="asc", limit=10000)
            chunk_id = digest({"route": "bars", "params": params})
            name = "bars/" + chunk_id + ".jsonl.gz"
            if name not in checksums or chunk_id not in checkpoint["chunks"]:
                raise SafetyError("D2_PARTITION_REFERENCE_MISSING")
            chosen.append(dict(path=name, sha256=checksums[name], symbol=symbol,
                               first_session=str(min(sessions)), last_session=str(max(sessions)),
                               session_dates=[str(d) for d in sessions], chunk_id=chunk_id))
        month = following
    return chosen


def map_rows(rows, item, calendar_by_day, counters):
    """Exact text to Bar, with no filling, reordering, or price conversion via float."""
    boundary(item["first_session"], item["last_session"])
    if item["symbol"] not in UNIVERSE:
        raise SafetyError("D2_UNIVERSE")
    previous = None
    mapped = {}
    for row in rows:
        counters["historical_price_rows_read"] += 1
        if set(row) != {"t", "o", "h", "l", "c", "v"} or not all(isinstance(row[k], str) for k in row):
            raise SafetyError("D2_EXACT_TEXT_SCHEMA")
        stamp = datetime.fromisoformat(row["t"].replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            raise SafetyError("D2_TIMESTAMP")
        day = stamp.astimezone(NY).date().isoformat()
        require_development_date(day)
        if day not in item["session_dates"] or day not in calendar_by_day:
            raise SafetyError("D2_OBJECT_DATE_MISMATCH")
        if previous is not None and stamp <= previous:
            raise SafetyError("D2_DUPLICATE_OR_ORDER")
        previous = stamp
        bar = Bar(stamp, *(row[k] for k in ("o", "h", "l", "c", "v")))
        opened, closed = calendar_session(calendar_by_day[day])
        if not opened <= bar.start < bar.end <= closed:
            raise SafetyError("D2_OUTSIDE_REGULAR_SESSION")
        mapped.setdefault(day, []).append(bar)
    return mapped


def make_session(row, bars, mask):
    require_development_date(row["date"])
    if sorted(bars) != list(UNIVERSE) or mask["universe"] != list(UNIVERSE):
        raise SafetyError("D2_REQUIRED_UNIVERSE")
    opened, closed = calendar_session(row)
    excluded = {x["symbol"] for x in mask["sessions"] if x["session"] == row["date"] and x["symbol"] in UNIVERSE}
    halts = []
    for h in mask["halts"]:
        a, b = datetime.fromisoformat(h["start"]), datetime.fromisoformat(h["end"])
        if a.astimezone(NY).date().isoformat() == row["date"]:
            halts.append({**h, "start": a, "end": b})
    return HistoricalDevelopmentSession(opened=opened, closed=closed, bars=bars,
                                        excluded=excluded, halts=halts)


class DevelopmentArchive:
    def __init__(self, parent, items, calendar, mask, access):
        self.parent, self.items, self.mask, self.access = parent, items, mask, access
        self.calendar = {r["date"]: r for r in calendar if "2016-01-04" <= r["date"] <= "2021-12-31"}

    def structural_audit(self):
        """Phase B only: decoded bars and structural counts; no core/session evaluation."""
        bars = {day: {s: [] for s in UNIVERSE} for day in self.calendar}
        counts = Counter()
        opened_paths = []
        for item in self.items:
            boundary(item["first_session"], item["last_session"])
            path = self.parent / item["path"]
            if path.is_symlink() or not path.resolve().is_relative_to(self.parent.resolve()):
                raise SafetyError("D2_UNSAFE_OBJECT_PATH")
            self.access.allow_price(path)
            raw = path.read_bytes()
            opened_paths.append(item["path"])
            if hashlib.sha256(raw).hexdigest() != item["sha256"]:
                raise SafetyError("D2_OBJECT_HASH_MISMATCH")
            # Only objects already proven to contain authorized regular sessions.
            rows = (json.loads(line) for line in gzip.decompress(raw).splitlines())
            for day, values in map_rows(rows, item, self.calendar, self.access.counters).items():
                if bars[day][item["symbol"]]:
                    raise SafetyError("D2_DUPLICATE_OBJECT")
                bars[day][item["symbol"]] = values
                counts[item["symbol"]] += len(values)
        missing = Counter()
        for day, symbols in bars.items():
            opened, closed = calendar_session(self.calendar[day])
            for symbol, values in symbols.items():
                existing = {b.start for b in values}
                t = opened
                while t < closed:
                    if t not in existing:
                        missing[symbol] += 1
                        excluded = any(x["symbol"] == symbol and x["session"] == day for x in self.mask["sessions"])
                        halts = [(datetime.fromisoformat(x["start"]), datetime.fromisoformat(x["end"])) for x in self.mask["halts"]]
                        if not excluded and not fully_halted(t, t + STEP, halts):
                            raise SafetyError("D2_UNMASKED_MISSING_SLOT")
                    t += STEP
        rows = sum(counts.values())
        report = {"status": "PASS", "source_objects_opened": len(opened_paths), "objects": opened_paths,
                  "decoded_bar_rows": rows, "rows_by_symbol": dict(counts), "sessions": len(bars),
                  "date_min": min(bars), "date_max": max(bars), "missing_slots_by_symbol": dict(missing),
                  "excluded_symbol_sessions": sum(x["symbol"] in UNIVERSE and x["session"] in bars for x in self.mask["sessions"]),
                  "early_close_sessions": sum((calendar_session(r)[1]-calendar_session(r)[0]).total_seconds() < 23400 for r in self.calendar.values()),
                  "halt_events": sum(x["start"][:10] in bars for x in self.mask["halts"]),
                  "duplicates": 0, "schema_order_grid_decimal_violations": 0, "unexpected_symbols": 0,
                  "global_unique_rows_decoded": rows, "row_identity": "parent_snapshot_id/symbol/UTC_bar_start",
                  "signals_calculated": 0, "returns_calculated": 0, "rows_exposed_to_strategy_at_audit": 0}
        return bars, report
