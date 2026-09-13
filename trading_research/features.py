"""Point-in-time features, using complete past windows only."""

from dataclasses import dataclass
from datetime import timedelta
from statistics import mean

from trading_runtime.market_calendar import aware


@dataclass(frozen=True)
class Features:
    symbol: str
    day: str
    at: object
    close: float
    open: float
    vwap: float
    opening_high: float
    recent_low: float
    high: float
    rising: bool
    intraday_return: float
    relative_return: float
    gap: float
    rvol: float
    dollar_volume: float
    last_volume: float
    regime: str


def vwap(rows):
    volume = sum(b.v for b in rows)
    # OHLC typical-price approximation, not provider trade-level VWAP.
    return sum((b.h + b.low + b.c) / 3 * b.v for b in rows) / volume


def build_features(dataset, symbol, day, offset, history_sessions=20):
    session = dataset.sessions[day]
    at = session.open + timedelta(minutes=offset)
    rows = dataset.window(symbol, day, at)
    benchmark = "QQQ" if symbol == "SPY" else "SPY"
    other = dataset.window(benchmark, day, at)
    market = dataset.window("SPY", day, at)
    index = dataset.days.index(day)
    past_days = dataset.days[max(0, index - history_sessions) : index]
    if (
        not rows
        or len(rows) < 6
        or not other
        or not market
        or len(past_days) != history_sessions
    ):
        return None
    past_full, past_partial = [], []
    for previous in past_days:
        s = dataset.sessions[previous]
        full = dataset.window(symbol, previous, s.close)
        partial = dataset.window(symbol, previous, s.open + timedelta(minutes=offset))
        if not full or not partial:
            return None
        past_full.append(full)
        past_partial.append(partial)
    previous_close = past_full[-1][-1].c
    gap = rows[0].o / previous_close - 1
    if abs(gap) > 0.2:  # Known heuristic, not claimed as a complete actions database.
        return None
    own_return = rows[-1].c / rows[0].o - 1
    other_return = other[-1].c / other[0].o - 1
    regime = (
        "favorable"
        if market[-1].c >= market[0].o and market[-1].c >= vwap(market)
        else "adverse"
    )
    return Features(
        symbol,
        day,
        at,
        rows[-1].c,
        rows[0].o,
        vwap(rows),
        max(b.h for b in rows[:6]),
        min(b.low for b in rows[-3:]),
        max(b.h for b in rows),
        rows[-1].c > rows[-2].c,
        own_return,
        own_return - other_return,
        gap,
        sum(b.v for b in rows) / mean(sum(b.v for b in p) for p in past_partial),
        mean(sum(b.v * b.c for b in p) for p in past_full),
        rows[-1].v,
        regime,
    )


def visible_news(items, at):
    # Revised articles are withheld until the revision was available.
    return [
        i
        for i in items
        if i.get("created_at")
        and i.get("updated_at")
        and max(aware(i["created_at"]), aware(i["updated_at"])) <= at
    ]
