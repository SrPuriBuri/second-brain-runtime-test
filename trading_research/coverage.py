"""Whole-archive integrity and action diagnostics; no strategy or return series."""

from collections import Counter
from datetime import timedelta

from trading_runtime.config import SafetyError
from trading_runtime.market_calendar import NY, session_from_row
from .archive import ACCEPTANCE, chunks
from .data_quality import audit_bars, utc_stamp
from .downloader import offline_references


def reconcile(symbol, rows, actions):
    daily = {}
    for row in rows:
        stamp = utc_stamp(row["t"])
        day = stamp.astimezone(NY).date().isoformat()
        daily.setdefault(day, []).append(row)
    relevant = [
        a
        for a in actions
        if symbol in (a["symbol"], a.get("old_symbol"), a.get("new_symbol"))
    ]
    unresolved, associated = [], []
    previous = None
    for day, values in sorted(daily.items()):
        current = float(values[0]["o"])
        events = [
            a for a in relevant if (a.get("ex_date") or a.get("effective_date")) == day
        ]
        splits = [
            a for a in events if "historical_price_multiplier" in a.get("terms", {})
        ]
        if previous and previous > 0:
            observed_ratio = current / previous
            if splits:
                expected = 1.0
                for event in splits:
                    expected *= float(event["terms"]["historical_price_multiplier"])
                item = {
                    "date": day,
                    "event_ids": [a["source_record_id"] for a in splits],
                    "observed_open_previous_close_ratio": observed_ratio,
                    "expected_split_factor": expected,
                }
                if (
                    abs(observed_ratio / expected - 1)
                    > ACCEPTANCE["split_ratio_relative_tolerance"]
                ):
                    unresolved.append(item)
                else:
                    associated.append(item)
            elif (
                abs(observed_ratio - 1)
                > ACCEPTANCE["suspicious_overnight_ratio_threshold"]
            ):
                unresolved.append(
                    {
                        "date": day,
                        "reason": "MATERIAL_DISCONTINUITY_REQUIRES_EVIDENCE",
                        "associated_types": [a["action_type"] for a in events],
                    }
                )
        previous = float(values[-1]["c"])
    identity_events = [
        a["source_record_id"]
        for a in relevant
        if a["action_type"]
        in {
            "name_changes",
            "cash_mergers",
            "stock_mergers",
            "stock_and_cash_mergers",
            "reorganizations",
            "worthless_removals",
            "spin_offs",
            "spinoffs",
            "redemptions",
        }
    ]
    unresolved.extend(
        {"event_id": x, "reason": "IDENTITY_EVENT_REQUIRES_MANUAL_RESOLUTION"}
        for x in identity_events
    )
    for event in relevant:
        if "historical_price_multiplier" in event.get("terms", {}) and not any(
            event["source_record_id"] in row.get("event_ids", [])
            for row in associated + unresolved
        ):
            unresolved.append(
                {
                    "event_id": event["source_record_id"],
                    "reason": "SPLIT_WITHOUT_MATCHED_BAR_WINDOW",
                }
            )
    return {
        "status": "UNRESOLVED" if unresolved else "PASS_WITH_LIMITATIONS",
        "counts": dict(Counter(a["action_type"] for a in relevant)),
        "matched_splits": associated,
        "unresolved": unresolved,
        "knowledge_time_verified": False,
        "limitation": "Provider action completeness and original announcement vintages not independently guaranteed; dividend gaps are not automatically errors.",
    }


def audit_archive(archive, identity_evidence):
    calendar, actions, assets = offline_references(archive)
    if not calendar:
        raise SafetyError("ARCHIVE_CALENDAR_MISSING")
    symbols = archive.plan["selection"]["symbols"]
    by_symbol = {s: [] for s in symbols}
    source_counts = {s: Counter() for s in symbols}
    completed = archive.index()["chunks"]
    for planned in chunks(archive.plan):
        if planned["id"] not in completed:
            continue
        ref = completed[planned["id"]]
        chunk = archive.read_object(ref)
        symbol = chunk["chunk"]["params"]["symbols"]
        by_symbol[symbol].extend(chunk["rows"])
        source_counts[symbol].update(chunk["quality"]["symbols"][symbol]["counts"])
    result, firsts, identity_ok = {}, [], True
    active = {r["symbol"]: r for r in assets}
    for symbol, rows in by_symbol.items():
        # Request/chunk order is deterministic. Do not sort away provider ordering defects.
        observed_quality = audit_bars({symbol: rows}, calendar, [symbol])["symbols"][
            symbol
        ]
        observed_quality["source_counts"] = dict(source_counts[symbol])
        first = min(
            (utc_stamp(r["t"]).astimezone(NY).date().isoformat() for r in rows),
            default=None,
        )
        firsts.append(first)
        present = {utc_stamp(r["t"]) for r in rows}
        longest = current = 0
        for session_row in calendar:
            session = session_from_row(session_row)
            stamp = session.open
            while stamp < session.close:
                current = 0 if stamp in present else current + 1
                longest = max(longest, current)
                stamp += timedelta(minutes=5)
        evidence = identity_evidence.get(symbol, {})
        identity = bool(
            evidence.get("etf_verified") is True
            and evidence.get("identity_continuity_verified") is True
            and evidence.get("source_ref")
            and evidence.get("asset_id") == active.get(symbol, {}).get("id")
            and active.get(symbol, {}).get("tradable") is True
            and active.get(symbol, {}).get("status") == "active"
        )
        identity_ok &= identity
        action = reconcile(symbol, rows, actions)
        clean = not any(
            source_counts[symbol][k]
            for k in (
                "duplicates",
                "out_of_order",
                "invalid_timestamp",
                "invalid_ohlcv",
                "off_grid",
            )
        )
        adequate = bool(
            observed_quality["completeness"] is not None
            and observed_quality["completeness"]
            >= ACCEPTANCE["min_symbol_completeness"]
            and longest <= ACCEPTANCE["max_unexplained_consecutive_missing_slots"]
        )
        result[symbol] = {
            **observed_quality,
            "first_usable_session": first,
            "expected_sessions": len(calendar),
            "observed_sessions": sum(
                s["observed"] > 0 for s in observed_quality["sessions"].values()
            ),
            "longest_missing_sequence": longest,
            "identity_verified": identity,
            "actions": action,
            "suitable": identity
            and clean
            and adequate
            and action["status"] != "UNRESOLVED",
        }
    status = archive.status()
    return {
        "symbols": result,
        "accepted": all(r["suitable"] for r in result.values())
        and status["completed_chunks"] == status["expected_chunks"]
        and status["OOS_price_requests"] == 0,
        "identity_verified": identity_ok,
        "common_coverage_start": max(firsts) if all(firsts) else None,
        "common_start_rule": "max first usable observed session; no return-based choice; acceptance still checks full archive coverage",
        "OOS_price_requests": status["OOS_price_requests"],
        "strategy_return_calculations": 0,
        "acceptance": ACCEPTANCE,
        "missingness_attribution": "UNKNOWN unless independently evidenced; no IEX filling, inferred outage, or inferred early close",
        "raw_archive_start": archive.plan["selection"]["archive_start"],
    }
