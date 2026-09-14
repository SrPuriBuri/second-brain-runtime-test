"""Evidence-backed listing intervals with an explicit knowledge-time boundary."""

from dataclasses import dataclass
from datetime import date, datetime, timezone

from trading_runtime.config import SafetyError
from trading_runtime.market_calendar import aware, NY


@dataclass(frozen=True)
class ListingInterval:
    security_id: str
    symbol: str
    listing_start: str
    listing_end: str | None
    exchange: str
    security_type: str
    known_at: str
    source_ref: str
    delisted: bool = False

    def __post_init__(self):
        start = date.fromisoformat(self.listing_start)
        if self.listing_end and date.fromisoformat(self.listing_end) <= start:
            raise SafetyError("INVALID_LISTING_INTERVAL")
        aware(self.known_at)
        if not all(
            (
                self.security_id,
                self.symbol,
                self.source_ref,
                self.exchange,
                self.security_type,
            )
        ):
            raise SafetyError("LISTING_EVIDENCE_REQUIRED")


def universe_at(records, at, supports_point_in_time=False):
    if not supports_point_in_time:
        raise SafetyError("POINT_IN_TIME_UNSUPPORTED_CURRENT_SNAPSHOT_FORBIDDEN")
    instant = aware(at).astimezone(timezone.utc)
    day = instant.astimezone(NY).date().isoformat()
    visible = [r for r in records if aware(r.known_at) <= instant]
    latest = {}
    for row in visible:
        key = (row.security_id, row.symbol, row.listing_start)
        previous = latest.get(key)
        if (
            previous
            and aware(row.known_at) == aware(previous.known_at)
            and row != previous
        ):
            raise SafetyError("CONFLICTING_LISTING_VINTAGES")
        if previous is None or aware(row.known_at) > aware(previous.known_at):
            latest[key] = row
    selected = [
        r
        for r in latest.values()
        if r.listing_start <= day and (r.listing_end is None or day < r.listing_end)
    ]
    if len({r.symbol for r in selected}) != len(selected) or len(
        {r.security_id for r in selected}
    ) != len(selected):
        raise SafetyError("AMBIGUOUS_LISTING_INTERVALS")
    return tuple(sorted(selected, key=lambda r: r.symbol))


def apply_symbol_change(old, event):
    effective, known = event.get("effective_date"), event.get("known_at")
    if (
        not effective
        or not known
        or event.get("old_symbol") != old.symbol
        or not event.get("new_symbol")
    ):
        raise SafetyError("SYMBOL_CHANGE_PIT_EVIDENCE_MISSING")
    # Caller retains the earlier vintage; never mutate it with a future delisting.
    closed = ListingInterval(
        old.security_id,
        old.symbol,
        old.listing_start,
        effective,
        old.exchange,
        old.security_type,
        known,
        event["source_record_id"],
        True,
    )
    opened = ListingInterval(
        old.security_id,
        event["new_symbol"],
        effective,
        None,
        old.exchange,
        old.security_type,
        known,
        event["source_record_id"],
    )
    return closed, opened


def current_asset_summary(rows):
    return {
        "count": len(rows),
        "supports_point_in_time": False,
        "listing_interval_status": "UNAVAILABLE_FROM_CURRENT_ASSETS",
        "inactive_meaning": "NOT_PROOF_OF_DELISTING",
        "observed_at": datetime.now(timezone.utc).isoformat(),
    }
