"""Manual data foundation audit. No research engine or performance entry point."""

from collections import Counter
from dataclasses import asdict
from datetime import datetime, timezone
import json
import os
from pathlib import Path

from trading_runtime.config import Config, SafetyError, redact
from trading_runtime.private_store import PrivateRepoStore, ROOT
from .reports import safety
from .dataset_cache import DatasetCache
from .dataset_manifest import sha256
from .providers.alpaca import AlpacaFoundationProvider, ROUTES
from .corporate_actions import normalize_alpaca
from .data_quality import audit_bars, split_adjustment_check
from .security_master import current_asset_summary

# Coverage/event/DST samples fixed before fetching prices. Entirely before OOS.
SAMPLES = (
    ("early_history", "2016-01-04", "2016-01-09", ("SPY", "QQQ")),
    ("v1_warmup_early_close", "2019-11-25", "2019-12-03", ("SPY", "QQQ")),
    ("v1_2020_gap", "2020-01-06", "2020-01-11", ("SPY", "QQQ")),
    ("split_event", "2020-08-27", "2020-09-04", ("AAPL", "TSLA")),
    ("rename", "2022-06-06", "2022-06-11", ("FB", "META")),
    ("acquired", "2022-10-24", "2022-11-03", ("TWTR",)),
    ("bank_failure", "2023-03-06", "2023-03-18", ("SIVB", "SIVBQ")),
    ("spring_dst", "2024-03-08", "2024-03-13", ("SPY", "QQQ", "XLK", "XLF")),
    ("fall_dst", "2024-11-01", "2024-11-06", ("SPY", "QQQ", "XLK", "XLF")),
    ("early_close", "2024-11-25", "2024-12-03", ("SPY", "QQQ", "XLK", "XLF")),
)
ACTION_SYMBOLS = (
    "SPY",
    "QQQ",
    "AAPL",
    "TSLA",
    "NVDA",
    "GE",
    "T",
    "WBD",
    "FB",
    "META",
    "TWTR",
    "SIVB",
    "SIVBQ",
)


def attempt(operation):
    try:
        return {"status": "OBSERVED", "data": operation()}
    except SafetyError as error:
        return {"status": str(error)}


def audit(provider):
    now = datetime.now(timezone.utc).isoformat()
    raw_actions = attempt(
        lambda: provider.corporate_actions(ACTION_SYMBOLS, "2016-01-01", "2024-12-31")
    )
    normalized, errors = [], Counter()
    if raw_actions["status"] == "OBSERVED":
        for kind, rows in raw_actions["data"].items():
            for row in rows:
                try:
                    normalized.append(normalize_alpaca(kind, row, now))
                except (SafetyError, ValueError, TypeError, KeyError):
                    errors[kind] += 1
    action_report = {
        "status": raw_actions["status"],
        "normalized_count": len(normalized),
        "types": dict(Counter(e["action_type"] for e in normalized)),
        "normalization_failures": dict(errors),
        "version": provider.action_version,
        "date_filter": "process_date_not_ex_date",
        "known_at_verified": False,
        "announcement_time_guarantee": False,
        "field_presence": {
            key: sum(e[key] is not None for e in normalized)
            for key in (
                "announcement_date",
                "effective_date",
                "ex_date",
                "record_date",
                "payment_date",
                "process_date",
            )
        },
        "samples_by_type": {
            kind: [e for e in normalized if e["action_type"] == kind][:2]
            for kind in sorted({e["action_type"] for e in normalized})
        },
        "by_year": dict(
            Counter((e["process_date"] or "UNKNOWN")[:4] for e in normalized)
        ),
        "event_verification": {
            symbol: [
                e
                for e in normalized
                if e["symbol"] == symbol
                and (e["ex_date"] or e["effective_date"] or e["process_date"] or "")[:4]
                in {"2020", "2021", "2022", "2023"}
            ][:8]
            for symbol in ("AAPL", "TSLA", "GE", "FB", "TWTR", "SIVB")
        },
    }
    quality, split_raw = [], {}
    for name, start, end, symbols in SAMPLES:
        calendar = provider.calendar(start, end)
        # End is exclusive for price windows; remove a calendar endpoint day.
        calendar = [row for row in calendar if row["date"] < end]
        for feed in ("iex", "sip"):
            result = attempt(lambda: provider.bars(symbols, start, end, feed))
            entry = {
                "sample": name,
                "start": start,
                "end_exclusive": end,
                "feed": feed,
                "requested_symbols": symbols,
                "status": result["status"],
            }
            if result["status"] == "OBSERVED":
                if name == "split_event" and feed == "sip":
                    split_raw = result["data"]
                entry["quality"] = audit_bars(result["data"], calendar, symbols)
                entry["content_sha256"] = sha256(result["data"])
            quality.append(entry)
    adjustments = {}
    for mode in ("split", "dividend", "all"):
        result = attempt(
            lambda: provider.bars(("AAPL",), "2020-08-27", "2020-09-04", "sip", mode)
        )
        adjustments[mode] = {
            "status": result["status"],
            "counts": {s: len(v) for s, v in result.get("data", {}).items()},
            "content_sha256": sha256(result["data"]) if "data" in result else None,
        }
        if mode == "split" and "data" in result:
            adjustments[mode]["event_consistency"] = split_adjustment_check(
                split_raw,
                result["data"],
                [
                    e
                    for e in normalized
                    if e["symbol"] == "AAPL"
                    and e["action_type"] in {"forward_splits", "reverse_splits"}
                ],
            )
    assets = {}
    for status in ("active", "inactive"):
        result = attempt(lambda: provider.assets(status))
        rows = result.get("data", [])
        assets[status] = {
            "status": result["status"],
            **current_asset_summary(rows),
            "fields": sorted({k for row in rows for k in row}),
            "requested_samples": [
                {
                    k: row.get(k)
                    for k in ("symbol", "id", "status", "exchange", "class", "tradable")
                }
                for row in rows
                if row.get("symbol") in ACTION_SYMBOLS
            ],
        }
    news = {
        str(year): attempt(
            lambda year=year: provider.news(("AAPL",), f"{year}-01-01", f"{year}-01-10")
        )
        for year in (2015, 2019, 2020, 2024)
    }
    micro = {}
    for route in ("quotes", "trades"):
        for feed in ("iex", "sip"):
            result = attempt(
                lambda: provider.get(
                    route,
                    {
                        "symbols": "SPY",
                        "start": "2024-03-08T14:30:00Z",
                        "end": "2024-03-08T14:31:00Z",
                        "feed": feed,
                        "limit": 1,
                    },
                )
            )
            micro[route + "_" + feed] = {
                "status": result["status"],
                "returned_symbols": sorted(result.get("data", {}).get(route, {})),
                "content_sha256": sha256(result["data"]) if "data" in result else None,
            }
    return {
        "phase": "3V2-A",
        "scope": "DATA_FOUNDATION_ONLY",
        "retrieved_at": now,
        "capabilities": asdict(provider.capabilities),
        "effective_paper_calendar_url": ROUTES["calendar"],
        "corporate_actions": action_report,
        "bar_quality": quality,
        "adjustments": adjustments,
        "assets": assets,
        "news": news,
        "historical_quotes_trades": micro,
        "request_count": provider.requests,
        "receipts": provider.receipts,
        "snapshots": provider.snapshots,
        "performance_computed": False,
        "oos_price_rows_requested": 0,
        "strategy_searches": 0,
        "orders_submitted": 0,
        "orders_cancelled": 0,
        "positions_closed": 0,
        "archive_status": "IMMUTABLE_EPHEMERAL_SNAPSHOTS_NO_DURABLE_BACKEND_CONFIGURED",
    }


def main(argv=None):
    try:
        config = Config.from_env()
        store = PrivateRepoStore(config.pat)
        before = safety(store)
        run_id = os.environ["GITHUB_RUN_ID"]
        if not run_id.isdigit():
            raise SafetyError("FOUNDATION_RUN_ID_INVALID")
        relative = "data/evidence/research-v2/foundation_" + run_id + ".json"
        if store.read_file(ROOT + relative):
            raise SafetyError("FOUNDATION_RUN_ALREADY_FINALIZED")
        provider = AlpacaFoundationProvider(
            config, DatasetCache(Path("research_cache"))
        )
        report = audit(provider)
        report.update(
            {
                "safety_before": before,
                "safety_after": safety(store),
                "git_commit": os.getenv("GITHUB_SHA"),
                "github_run_id": run_id,
            }
        )
        clean = json.loads(redact(json.dumps(report, allow_nan=False)))
        if len(json.dumps(clean).encode()) > 850000:
            raise SafetyError("FOUNDATION_REPORT_TOO_LARGE")
        store.create_append_only_record(ROOT + relative, clean)
        safety(store)
        print(
            json.dumps(
                {"status": "DATA_FOUNDATION_EVIDENCE_PERSISTED", "broker_mutations": 0}
            )
        )
        return 0
    except Exception as error:
        print(
            json.dumps(
                {
                    "status": "FAILED",
                    "reason": str(error)
                    if isinstance(error, SafetyError)
                    else "FOUNDATION_FAILED",
                }
            )
        )
        return 1
