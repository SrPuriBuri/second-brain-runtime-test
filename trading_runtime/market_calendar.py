"""Actual Alpaca sessions, including early closes and US/EU DST divergence."""

from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from dateutil.parser import isoparse

from .config import SafetyError

NY = ZoneInfo("America/New_York")
MADRID = ZoneInfo("Europe/Madrid")


def aware(value):
    result = isoparse(value) if isinstance(value, str) else value
    if result.tzinfo is None:
        raise SafetyError("NAIVE_TIMESTAMP")
    return result


@dataclass(frozen=True)
class Session:
    trade_date: str
    open: datetime
    close: datetime


def get_session(broker, day):
    rows = broker.calendar(day)
    if not rows:
        return None
    if len(rows) != 1 or str(rows[0]["date"]) != str(day):
        raise SafetyError("INVALID_CALENDAR")
    row = rows[0]
    return session_from_row(row)


def session_from_row(row):
    day = str(row["date"])

    def local_time(value):
        # raw Alpaca calendar uses HH:MM; SDK models may use datetime.
        if isinstance(value, str) and "T" not in value and " " not in value:
            value = datetime.fromisoformat(f"{day}T{value}")
        elif isinstance(value, str):
            value = isoparse(value)
        return (
            value.replace(tzinfo=NY) if value.tzinfo is None else value.astimezone(NY)
        )

    session = Session(str(day), local_time(row["open"]), local_time(row["close"]))
    if session.open >= session.close:
        raise SafetyError("INVALID_CALENDAR")
    return session


def display_time(value):
    return {
        "America/New_York": value.astimezone(NY).isoformat(),
        "Europe/Madrid": value.astimezone(MADRID).isoformat(),
    }
