"""Short due windows prevent delayed cron from replaying old entry decisions."""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .market_calendar import MADRID

ENTRY_SLOTS = {"FIRST_SCAN", "LAST_NEW_TRADE"}


def slot_times(session, schedule):
    if session is None:
        return {}
    return {
        name: getattr(session, rule["anchor"])
        + timedelta(minutes=rule["offset_minutes"])
        for name, rule in schedule["slots"].items()
    }


def due_slots(now, session, schedule):
    window = timedelta(minutes=schedule["due_window_minutes"])
    times = slot_times(session, schedule)
    result = [
        name
        for name, at in times.items()
        if at <= now < at + window
        and (name not in ENTRY_SLOTS or now < times["FORCE_FLAT"])
    ]
    morning = schedule["MORNING_RESEARCH_ES"]
    local = now.astimezone(ZoneInfo(morning["timezone"]))
    at = datetime.fromisoformat(f"{local.date()}T{morning['time']}").replace(
        tzinfo=MADRID
    )
    if local.weekday() < 5 and at <= now < at + window:
        result.insert(0, "MORNING_RESEARCH_ES")
    return result


def run_id(day, slot, version):
    return f"{day}_{slot}_strategy-v{version}"
