"""Immutable synthetic-only D1 inputs. No archive reader or provider is exposed."""

from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType
from importlib.resources import files
from zoneinfo import ZoneInfo

from trading_research.v2_protocol import UNIVERSE
from trading_runtime.config import SafetyError
from .clock import STEP
from .numeric import exact_decimal

# Explicit tzdata source rather than an ambient machine timezone database.
with files("tzdata.zoneinfo.America").joinpath("New_York").open("rb") as _tz:
    NY = ZoneInfo.from_file(_tz, key="America/New_York")


def timestamp(text):
    value = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if value.tzinfo is None or value.year >= 2025:
        raise SafetyError("D1_TIMESTAMP_FORBIDDEN")
    return value


@dataclass(frozen=True)
class Bar:
    start: datetime
    open_text: str
    high_text: str
    low_text: str
    close_text: str
    volume_text: str

    def __post_init__(self):
        if self.start.tzinfo is None or self.start.year >= 2025:
            raise SafetyError("D1_TIMESTAMP_FORBIDDEN")
        if self.start.minute % 5 or self.start.second or self.start.microsecond:
            raise SafetyError("D1_OFF_GRID")
        o, h, lo, c, v = self.values
        if not (0 < lo <= min(o, c) <= max(o, c) <= h) or v <= 0:
            raise SafetyError("D1_INVALID_BAR")

    @property
    def values(self):
        return tuple(exact_decimal(x) for x in (self.open_text, self.high_text, self.low_text, self.close_text, self.volume_text))

    @property
    def end(self):
        return self.start + STEP


class SyntheticSession:
    """Explicit synthetic provenance. D1 has no real-data adapter or path loader."""

    def __init__(self, *, source, opened, closed, bars, halts=(), excluded=(), invalid_slots=()):
        if source != "SYNTHETIC_GOLDEN_D1":
            raise SafetyError("D1_REAL_DATA_FORBIDDEN")
        if opened.tzinfo is None or closed.tzinfo is None or opened >= closed:
            raise SafetyError("D1_INVALID_SESSION")
        if opened.year >= 2025 or closed.year >= 2025:
            raise SafetyError("D1_OOS_FORBIDDEN")
        if opened.astimezone(NY).date() != closed.astimezone(NY).date():
            raise SafetyError("D1_CROSS_DAY")
        copied = {}
        for symbol, rows in bars.items():
            if symbol not in UNIVERSE:
                raise SafetyError("D1_UNIVERSE")
            mapped = {}
            for bar in rows:
                if bar.start in mapped or not opened <= bar.start < bar.end <= closed:
                    raise SafetyError("D1_INVALID_SESSION_BAR")
                mapped[bar.start] = bar
            copied[symbol] = MappingProxyType(mapped)
        checked = []
        for event in halts:
            if not event.get("verified") or not event.get("source_ref"):
                raise SafetyError("D1_UNVERIFIED_HALT")
            a, b = event["start"], event["end"]
            if not opened <= a < b <= closed:
                raise SafetyError("D1_INVALID_HALT")
            checked.append((a, b))
        self.open = opened
        self.close = closed
        self.bars = MappingProxyType(copied)
        self.halts = tuple(sorted(checked))
        self.excluded = frozenset(excluded)
        self.invalid_slots = frozenset(invalid_slots)
        self.source = source

    def bar(self, symbol, at):
        if (symbol, at) in self.invalid_slots:
            return None
        return self.bars.get(symbol, {}).get(at)

    def visible(self, symbol, at):
        return tuple(b for _, b in sorted(self.bars.get(symbol, {}).items())
                     if b.end <= at and (symbol, b.start) not in self.invalid_slots)


def require_synthetic(session):
    if type(session) is not SyntheticSession or session.source != "SYNTHETIC_GOLDEN_D1":
        raise SafetyError("D1_REAL_DATA_FORBIDDEN")
