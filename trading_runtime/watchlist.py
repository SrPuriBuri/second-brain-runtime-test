"""Price, liquidity and freshness based research shortlist, never a trade signal."""

from .market_calendar import aware
from .models import Watchlist
from .risk import dec
from .universe import bounded_universe


def build_watchlist(assets, data, policy, strategy, now, day):
    symbols, rejections = bounded_universe(assets, policy)
    snapshots, feed, notes = data.snapshots(symbols)
    candidates = []
    for symbol in symbols:
        try:
            row = snapshots[symbol]
            quote, bar = row["latestQuote"], row["prevDailyBar"]
            bid, ask = dec(quote["bp"]), dec(quote["ap"])
            stamp = aware(quote["t"])
            volume = dec(bar["v"]) * dec(bar["c"])
            if (
                not dec(policy.min_price) <= ask <= dec(policy.max_price)
                or bid <= 0
                or ask < bid
            ):
                reason = "PRICE_BOUNDS"
            elif not 0 <= (now - stamp).total_seconds() <= policy.max_data_age_seconds:
                reason = "STALE_DATA"
            elif volume < dec(policy.min_daily_dollar_volume):
                reason = "LIQUIDITY_BOUNDS"
            elif (ask - bid) / ask > dec(policy.max_spread_fraction):
                reason = "SPREAD_TOO_WIDE"
            else:
                candidates.append(
                    {
                        "symbol": symbol,
                        "ask": float(ask),
                        "bid": float(bid),
                        "timestamp": stamp.isoformat(),
                        "feed": feed,
                        "previous_daily_dollar_volume": float(volume),
                        "liquidity_timestamp": bar["t"],
                        "reasons": [
                            "CONFIGURED_LIQUID_SEED",
                            "PRICE_LIQUIDITY_SPREAD_PASSED",
                        ],
                        "classification": "RESEARCH_CANDIDATE",
                    }
                )
                continue
        except (KeyError, TypeError, ValueError, ArithmeticError):
            reason = "INCOMPLETE_MARKET_EVIDENCE"
        rejections.append({"symbol": symbol, "reason": reason})
    candidates.sort(key=lambda c: (-c["previous_daily_dollar_volume"], c["symbol"]))
    for row in candidates[policy.watchlist_limit :]:
        rejections.append({"symbol": row["symbol"], "reason": "WATCHLIST_BOUND"})
    notes.extend(
        [
            "IEX is not consolidated SIP; volume and coverage are feed-specific.",
            "Fewer than five eligible symbols is acceptable; never pad with invalid candidates.",
        ]
    )
    return Watchlist(
        timestamp=now,
        trade_date=day,
        source="alpaca/snapshots+assets+configured-seeds",
        data_feed=feed,
        strategy_version=strategy.version,
        candidates=candidates[: policy.watchlist_limit],
        rejections=rejections,
        notes=notes,
    )
