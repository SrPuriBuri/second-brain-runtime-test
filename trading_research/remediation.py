"""Performance-independent market-state metadata and immutable child views."""

from collections import defaultdict
from bisect import bisect_left, bisect_right
from datetime import timedelta
from decimal import Decimal
import json
import os
from pathlib import Path
import tempfile

from trading_runtime.config import SafetyError
from trading_runtime.market_calendar import NY, session_from_row
from .archive import atomic_json, data_root, file_hash, filesystem_path
from .data_quality import pre_oos_interval, utc_stamp
from .dataset_manifest import sha256

STEP = timedelta(minutes=5)


def validate_query(query):
    p = query["params"]
    pre_oos_interval(p["start"], p["end"])
    if query["route"] not in {"bars", "trades"} or not query.get("purpose"):
        raise SafetyError("REMEDIATION_QUERY_INVALID")
    if not 0 < (utc_stamp(p["end"]) - utc_stamp(p["start"])).total_seconds() <= 86400:
        raise SafetyError("REMEDIATION_WINDOW_TOO_WIDE")
    if (
        p.get("feed") not in {"sip", "iex"}
        or "," in p["symbols"]
        or p.get("limit") != 10000
    ):
        raise SafetyError("REMEDIATION_QUERY_INVALID")
    if query["route"] == "bars" and (
        p.get("timeframe") not in {"1Min", "5Min"} or p.get("adjustment") != "raw"
    ):
        raise SafetyError("REMEDIATION_QUERY_INVALID")


def scheduled_slots(calendar):
    for row in calendar:
        session = session_from_row(row)
        pre_oos_interval(row["date"], row["date"])
        stamp = session.open
        while stamp < session.close:
            yield stamp.astimezone(utc_stamp("2024-01-01T00:00:00Z").tzinfo)
            stamp += STEP


def halted_slot(stamp, halts):
    stamp = utc_stamp(stamp)
    for event in halts:
        if not event.get("source_ref") or not event.get("verified"):
            raise SafetyError("REMEDIATION_UNSOURCED_HALT")
        start, end = utc_stamp(event["start"]), utc_stamp(event["end"])
        pre_oos_interval(event["start"], event["end"])
        if start <= stamp and stamp + STEP <= end:
            return event["id"]
    return None


def gap_inventory(coverage, calendar):
    slots = list(scheduled_slots(calendar))
    missing = {}
    correlation = defaultdict(list)
    for symbol, quality in sorted(coverage["symbols"].items()):
        found = set()
        for gap in quality["gaps"]:
            start, end = utc_stamp(gap["start"]), utc_stamp(gap["end"])
            found.update(slots[bisect_left(slots, start) : bisect_right(slots, end)])
        if len(found) != quality["missing"]:
            raise SafetyError("REMEDIATION_GAP_COUNT_MISMATCH")
        missing[symbol] = [t.isoformat() for t in sorted(found)]
        for stamp in sorted(found):
            correlation[stamp.isoformat()].append(symbol)
    return {
        "missing": missing,
        "correlation": [
            {"timestamp": t, "symbols": s, "count": len(s)}
            for t, s in sorted(correlation.items())
        ],
    }


def normalize_in_kind(event):
    required = ("symbol", "distributed_symbol", "ex_date", "ratio", "source_ref")
    if not all(event.get(k) for k in required) or Decimal(event["ratio"]) <= 0:
        raise SafetyError("REMEDIATION_ACTION_INVALID")
    pre_oos_interval(event["ex_date"], event["ex_date"])
    return {
        **event,
        "action_type": "ETF_IN_KIND_DISTRIBUTION",
        "share_ratio": str(Decimal(event["ratio"])),
        "is_cash_dividend": False,
        "knowledge_time_verified": False,
        "cross_day_adjustment_validated": False,
    }


def build_mask(inventory, calendar, halts, identity, actions, provider_slots=None):
    provider_slots = provider_slots or {}
    slots = list(scheduled_slots(calendar))
    halted = {
        t.isoformat(): halted_slot(t, halts) for t in slots if halted_slot(t, halts)
    }
    sessions = len(calendar)
    classifications, exclusions, universe, gates = [], [], [], {}
    for symbol, absent in sorted(inventory["missing"].items()):
        nonhalt = [t for t in absent if t not in halted]
        days = sorted({utc_stamp(t).astimezone(NY).date().isoformat() for t in nonhalt})
        completeness = 1 - len(nonhalt) / (len(slots) - len(halted))
        eligible = (
            completeness >= 0.999
            and len(days) / sessions <= 0.005
            and identity[symbol]["status"] in {"VERIFIED", "VERIFIED_WITH_LIMITATIONS"}
        )
        gates[symbol] = {
            "pre_mask_completeness": completeness,
            "excluded_sessions": len(days),
            "excluded_session_fraction": len(days) / sessions,
            "eligible": eligible,
        }
        if eligible:
            universe.append(symbol)
        for stamp in absent:
            cause = (
                "MARKET_WIDE_CIRCUIT_BREAKER"
                if stamp in halted
                else provider_slots.get(symbol + "/" + stamp, {}).get(
                    "classification", "UNRESOLVED"
                )
            )
            classifications.append(
                {
                    "symbol": symbol,
                    "timestamp": stamp,
                    "classification": cause,
                    "treatment": "VERIFIED_NON_TRADING_INTERVAL"
                    if stamp in halted
                    else "SYMBOL_SESSION_EXCLUDED",
                    "evidence": halted.get(stamp)
                    or provider_slots.get(symbol + "/" + stamp, {}).get("evidence"),
                }
            )
        exclusions.extend(
            {
                "symbol": symbol,
                "session": day,
                "status": "INVALID_DATA_GAP",
                "reason": "NON_HALT_MISSING_OBSERVATION",
                "source_evidence": "parent coverage and dated classifications",
            }
            for day in days
        )
    return {
        "schema_version": 1,
        "universe": universe,
        "excluded_symbols": sorted(set(inventory["missing"]) - set(universe)),
        "gates": gates,
        "default": "VALID_ONLY_WITHIN_INCLUDED_SYMBOL_SCHEDULED_SESSION",
        "sessions": exclusions,
        "halts": halts,
        "non_trading_slots": sorted(halted),
        "classifications": classifications,
        "cross_day_action_boundaries": [
            {
                "symbol": a["symbol"],
                "date": a["ex_date"],
                "status": "INVALID_CROSS_DAY_ACTION_ADJUSTMENT",
                "source_evidence": a["source_ref"],
            }
            for a in actions
        ],
        "fills": 0,
        "interpolation": False,
        "strategy_return_calculations": 0,
    }


def validity(mask, symbol, stamp, calendar, window_start=None):
    t = utc_stamp(stamp)
    pre_oos_interval(stamp, stamp)
    if symbol not in mask["universe"]:
        return "INVALID_EXCLUDED_SYMBOL"
    day = t.astimezone(NY).date().isoformat()
    session = next((session_from_row(r) for r in calendar if r["date"] == day), None)
    if session is None or not session.open <= t < session.close:
        return "NON_TRADING_SCHEDULED"
    if (t - session.open).total_seconds() % STEP.total_seconds():
        return "INVALID_OFF_GRID"
    if any(e["symbol"] == symbol and e["session"] == day for e in mask["sessions"]):
        return "INVALID_DATA_GAP"
    if halted_slot(t, mask["halts"]):
        return "NON_TRADING_MWCB"
    if window_start:
        pre_oos_interval(window_start, stamp)
        first = utc_stamp(window_start).astimezone(NY).date().isoformat()
        if any(
            a["symbol"] == symbol and first < a["date"] <= day
            for a in mask["cross_day_action_boundaries"]
        ):
            return "INVALID_CROSS_DAY_ACTION_ADJUSTMENT"
        if any(
            e["symbol"] == symbol and first <= e["session"] <= day
            for e in mask["sessions"]
        ):
            return "INVALID_DATA_GAP"
    return "VALID"


def freeze_view(root, parent, protocol_hash, mask, actions, identity):
    root = filesystem_path(data_root({"AIST_DATA_ROOT": str(root)}))
    parent = Path(parent).resolve()
    manifest = json.loads((parent / "manifest.json").read_text())
    payload = {
        "schema_version": 1,
        "parent_dataset_id": manifest["dataset_id"],
        "parent_snapshot_id": manifest["snapshot_id"],
        "parent_manifest_hash": file_hash(parent / "manifest.json"),
        "parent_checksums_hash": file_hash(parent / "checksums.json"),
        "protocol_hash": protocol_hash,
        "mask": mask,
        "actions": actions,
        "identity": identity,
        "raw_bars_duplicated": False,
        "strategy_research_authorized": False,
    }
    view_id = sha256(payload)
    directory = root / "ai-stock-trader/research-views" / view_id
    if directory.exists():
        if json.loads((directory / "view.json").read_text()) != payload:
            raise SafetyError("REMEDIATION_VIEW_CONFLICT")
        return view_id
    directory.parent.mkdir(parents=True, exist_ok=True)
    temp = Path(tempfile.mkdtemp(prefix=".partial-", dir=directory.parent))
    atomic_json(temp / "view.json", payload)
    atomic_json(
        temp / "manifest.json",
        {
            "view_id": view_id,
            "content_hash": sha256(payload),
            "state": "FROZEN_METADATA_ONLY",
            "parent_snapshot_id": manifest["snapshot_id"],
        },
    )
    os.rename(temp, directory)
    return view_id


def verify_view(directory, parent):
    """Verify metadata identity and pinned parent files; never modify either."""
    directory, parent = Path(directory), Path(parent)
    try:
        payload = json.loads((directory / "view.json").read_text())
        manifest = json.loads((directory / "manifest.json").read_text())
        parent_manifest = json.loads((parent / "manifest.json").read_text())
        if (
            directory.name.startswith(".partial-")
            or directory.name != sha256(payload)
            or manifest["view_id"] != directory.name
            or manifest["content_hash"] != directory.name
            or manifest["state"] != "FROZEN_METADATA_ONLY"
            or manifest["parent_snapshot_id"] != payload["parent_snapshot_id"]
            or payload["parent_snapshot_id"] != parent_manifest["snapshot_id"]
            or payload["parent_dataset_id"] != parent_manifest["dataset_id"]
            or payload["parent_manifest_hash"] != file_hash(parent / "manifest.json")
            or payload["parent_checksums_hash"] != file_hash(parent / "checksums.json")
            or payload["raw_bars_duplicated"] is not False
            or payload["strategy_research_authorized"] is not False
        ):
            raise ValueError()
        return {
            "result": "VIEW_VERIFY_PASS",
            "view_id": directory.name,
            "parent_snapshot_id": payload["parent_snapshot_id"],
            "provider_calls": 0,
        }
    except (OSError, ValueError, KeyError, TypeError):
        raise SafetyError("REMEDIATION_VIEW_INVALID") from None
