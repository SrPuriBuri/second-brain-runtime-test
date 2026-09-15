"""Offline native-page lineage and complete timestamp/quality audit. No HTTP client."""

from collections import Counter
from datetime import timedelta
from decimal import Decimal, InvalidOperation
import gzip
import json

from trading_runtime.config import SafetyError
from trading_runtime.market_calendar import NY, session_from_row
from .archive import chunks, file_hash, normalize_bar
from .corporate_actions import normalize_alpaca
from .data_quality import pre_oos_interval, utc_stamp
from .dataset_manifest import sha256


def require(condition, code):
    if not condition:
        raise SafetyError(code)


def verify_receipts(index, page_provenance):
    successes = Counter()
    statuses = Counter()
    for receipt in index["receipts"]:
        key = receipt["request_hash"]
        require(key in page_provenance, "FINALIZATION_RECEIPT_REQUEST")
        statuses[str(receipt["http_status"])] += 1
        if receipt["http_status"] == 200:
            require(
                receipt["content_sha256"] == page_provenance[key]["content_hash"],
                "FINALIZATION_RECEIPT_HASH",
            )
            successes[key] += 1
    require(set(successes) == set(page_provenance), "FINALIZATION_RECEIPT_MISSING")
    return {
        "receipt_statuses": dict(statuses),
        "successful_attempts": sum(successes.values()),
        "unsuccessful_attempts": sum(v for k, v in statuses.items() if k != "200"),
        "repeated_successful_requests": sum(v - 1 for v in successes.values()),
        "resume_events": None,
        "resume_events_limitation": "Checkpoint receipts do not record process start/resume events.",
    }


def row_defects(rows):
    result = Counter()
    previous, seen = None, set()
    for row in rows:
        result["rows"] += 1
        try:
            stamp = utc_stamp(row["t"])
        except (ValueError, TypeError, KeyError, SafetyError):
            result["invalid_timestamps"] += 1
            continue
        require(stamp.year < 2025, "FINALIZATION_OOS_ROW")
        result["duplicates"] += stamp in seen
        result["out_of_order"] += previous is not None and stamp < previous
        result["off_grid"] += bool(
            stamp.minute % 5 or stamp.second or stamp.microsecond
        )
        previous = stamp
        seen.add(stamp)
        try:
            o, h, low, c = (Decimal(str(row[k])) for k in ("o", "h", "l", "c"))
            finite = all(x.is_finite() for x in (o, h, low, c))
            result["invalid_prices"] += not finite or any(
                x <= 0 for x in (o, h, low, c)
            )
            result["ohlc_violations"] += (
                not finite or not low <= min(o, c) <= max(o, c) <= h
            )
            volume = Decimal(str(row["v"]))
            result["invalid_volumes"] += not volume.is_finite() or volume < 0
            result["zero_volume"] += volume.is_finite() and volume == 0
        except (InvalidOperation, ValueError, KeyError):
            result["unparseable_ohlcv"] += 1
    return dict(result)


def verify_completed(archive, progress=None):
    """Verify every referenced object and page chain, without treating gaps as corruption."""
    index = archive.index()
    planned = list(chunks(archive.plan))
    require(
        set(index["chunks"]) == {c["id"] for c in planned}, "FINALIZATION_CHUNK_SET"
    )
    require(index["OOS_price_requests"] == 0, "FINALIZATION_OOS_REQUEST")
    object_files, bytes_by_kind, page_refs = {}, Counter(), {}
    calendar, actions, assets = [], [], []
    used = set()

    def read(ref, kind):
        value = archive.read_object(ref)
        directory = archive.cache._path(
            ref["domain"], ref["dataset_id"], ref["snapshot_id"]
        )
        for name in ("manifest.json", "payload.json.gz"):
            path = directory / name
            relative = path.relative_to(archive.path).as_posix()
            if relative not in object_files:
                object_files[relative] = file_hash(path)
                bytes_by_kind[kind + "_stored_bytes"] += path.stat().st_size
                if name.endswith(".gz"):
                    data = path.read_bytes()
                    bytes_by_kind[kind + "_compressed_payload_bytes"] += len(data)
                    bytes_by_kind[kind + "_uncompressed_payload_bytes"] += len(
                        gzip.decompress(data)
                    )
        return value

    def page(key):
        require(key in index["requests"], "FINALIZATION_PAGE_MISSING")
        value = read(index["requests"][key], "native")
        require(
            value["request_identity"]
            == key
            == sha256({"route": value["route"], "params": value["parameters"]}),
            "FINALIZATION_PAGE_ID",
        )
        require(
            value["content_hash"] == sha256(value["body"]), "FINALIZATION_BODY_HASH"
        )
        if value["route"] != "assets":
            pre_oos_interval(value["parameters"]["start"], value["parameters"]["end"])
        used.add(key)
        page_refs[key] = {
            "route": value["route"],
            "content_hash": value["content_hash"],
            "retrieved_at": value["retrieved_at"],
        }
        return value

    for key, ref in index["requests"].items():
        if ref["domain"] == "market":
            continue
        p = page(key)
        if p["route"] == "calendar":
            calendar.extend(p["body"])
        elif p["route"] == "assets":
            assets.extend(p["body"])
        elif p["route"] == "corporate_actions":
            require(
                not p["body"].get("next_page_token"),
                "FINALIZATION_ACTION_PAGINATION_REVIEW_REQUIRED",
            )
            for kind, values in p["body"].get("corporate_actions", {}).items():
                actions.extend(
                    normalize_alpaca(kind, r, p["retrieved_at"]) for r in values
                )
    sessions = {r["date"]: session_from_row(r) for r in calendar}
    require(len(sessions) == len(calendar) > 0, "FINALIZATION_CALENDAR")
    for day, session in sessions.items():
        require(
            day < "2025-01-01" and session.open < session.close,
            "FINALIZATION_CALENDAR_BOUNDARY",
        )
    symbols = {
        s: {
            "raw": Counter(),
            "canonical": Counter(),
            "timestamps": set(),
            "first_raw": None,
            "last_raw": None,
        }
        for s in archive.plan["selection"]["symbols"]
    }
    for number, chunk in enumerate(planned, 1):
        value = read(index["chunks"][chunk["id"]], "canonical")
        require(
            value["status"] == "COMPLETE"
            and value["chunk"] == chunk
            and value["row_count"] == len(value["rows"]),
            "FINALIZATION_CHUNK_METADATA",
        )
        params, native, cursors = dict(chunk["params"]), [], set()
        symbol = params["symbols"]
        while True:
            p = page(sha256({"route": "bars", "params": params}))
            body = p["body"]
            require(
                set(body.get("bars") or {}).issubset({symbol}), "FINALIZATION_SYMBOL"
            )
            native.extend((body.get("bars") or {}).get(symbol, []))
            cursor = body.get("next_page_token")
            if not cursor:
                break
            require(
                cursor not in cursors and len(cursors) < 100, "FINALIZATION_PAGE_LOOP"
            )
            cursors.add(cursor)
            params["page_token"] = cursor
        regular = []
        for row in native:
            stamp = utc_stamp(row["t"])
            require(
                stamp.year < 2025
                and utc_stamp(params["start"]) <= stamp <= utc_stamp(params["end"]),
                "FINALIZATION_RAW_BOUNDARY",
            )
            stats = symbols[symbol]
            iso = stamp.isoformat()
            stats["first_raw"] = min(stats["first_raw"] or iso, iso)
            stats["last_raw"] = max(stats["last_raw"] or iso, iso)
            session = sessions.get(stamp.astimezone(NY).date().isoformat())
            if session and session.open <= stamp < session.close:
                regular.append(normalize_bar(row))
        require(
            sha256(regular) == sha256(value["rows"]),
            "FINALIZATION_NATIVE_CANONICAL_LINEAGE",
        )
        stats = symbols[symbol]
        stats["raw"].update(row_defects(native))
        stats["canonical"].update(row_defects(regular))
        for row in regular:
            stamp = utc_stamp(row["t"])
            require(
                stamp not in stats["timestamps"], "FINALIZATION_CROSS_CHUNK_DUPLICATE"
            )
            stats["timestamps"].add(stamp)
        if progress and number % 126 == 0:
            progress(number)
    require(used == set(index["requests"]), "FINALIZATION_UNREFERENCED_REQUEST")
    for stats in symbols.values():
        present = stats.pop("timestamps")
        stats["first_regular"] = min(present).isoformat() if present else None
        stats["last_regular"] = max(present).isoformat() if present else None
        gaps, yearly, start, end, length = [], {}, None, None, 0
        for day, session in sorted(sessions.items()):
            year = yearly.setdefault(day[:4], Counter())
            stamp = session.open
            while stamp < session.close:
                year["expected"] += 1
                year["observed"] += stamp in present
                if stamp not in present:
                    start = start or stamp.isoformat()
                    end, length = stamp.isoformat(), length + 1
                elif start:
                    gaps.append(
                        {
                            "start": start,
                            "end": end,
                            "slots": length,
                            "cause": "UNKNOWN",
                        }
                    )
                    start, end, length = None, None, 0
                stamp += timedelta(minutes=5)
        if start:
            gaps.append(
                {"start": start, "end": end, "slots": length, "cause": "UNKNOWN"}
            )
        stats["gaps"] = gaps
        stats["yearly"] = yearly
        stats["canonical_outside_session"] = (
            0  # Exact equality with native session filtering proved above.
        )
    receipts = verify_receipts(index, page_refs)
    return {
        "dataset_id": archive.plan["dataset_id"],
        "result": "INTEGRITY_PASS",
        "completed_chunks": len(planned),
        "pages": len(used),
        "attempts": len(index["receipts"]),
        **receipts,
        "symbols": symbols,
        "calendar": calendar,
        "actions": actions,
        "assets": [a for a in assets if a["symbol"] in symbols],
        "bytes": dict(bytes_by_kind),
        "object_files": object_files,
        "object_files_hash": sha256(object_files),
        "page_provenance": page_refs,
        "checkpoint_hash": file_hash(archive.path / "checkpoint.json"),
        "plan_hash": file_hash(archive.path / "plan.json"),
        "OOS_price_requests": 0,
        "stored_oos_rows": 0,
        "strategy_return_calculations": 0,
    }


def main():
    import argparse
    from .archive import Archive, atomic_json, data_root, make_plan

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default="full-audit.json")
    args = parser.parse_args()
    archive = Archive(data_root(), make_plan())
    with archive.writer():
        result = verify_completed(
            archive, lambda n: print(json.dumps({"verified_chunks": n}), flush=True)
        )
        atomic_json(archive.path / args.output, result)
    print(
        json.dumps({"result": result["result"], "chunks": result["completed_chunks"]})
    )


if __name__ == "__main__":
    main()
