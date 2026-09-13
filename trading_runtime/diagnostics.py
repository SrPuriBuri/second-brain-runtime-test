"""Read-only capability audit. Raw broker responses and credentials never persist."""

from datetime import timedelta
import json
import os
from uuid import uuid4

from alpaca.trading.enums import AccountStatus

from .config import SafetyError, redact
from .market_calendar import NY, aware, display_time, session_from_row
from .private_store import ROOT
from .reconcile import reconcile
from .risk import dec
from .runner import Runner
from .simulation import dry_run
from .slots import slot_times
from .strategist import NoAI

SAMPLES = ("SPY", "QQQ", "AAPL", "MSFT", "NVDA", "AMD", "META", "AMZN", "GOOGL", "TSLA")
EXCHANGES = {"NYSE", "NASDAQ", "ARCA", "AMEX", "BATS", "OTC"}


def clean_document(value, env=None):
    """Defense in depth after field allowlisting; never print or persist raw errors."""
    return json.loads(redact(json.dumps(value, allow_nan=False), env))


def probe(data, path, params, field):
    """Probe the requested feed exactly; an entitlement probe must not fall back."""
    try:
        response = data.get(path, params)
        status = response.status_code
        if status != 200:
            code = {
                401: "AUTHENTICATION_FAILED",
                403: "FORBIDDEN_OR_NOT_ENTITLED",
                404: "NOT_AVAILABLE",
                429: "RATE_LIMITED",
            }.get(status, "HTTP_ERROR")
            return {"status": code, "http_status": status, "working": False}, None
        body = response.json()
        payload = body.get(field) if field else body
        expected = list if field in {"news", "most_actives", "gainers"} else dict
        if not isinstance(payload, expected):
            return {
                "status": "INVALID_RESPONSE",
                "http_status": 200,
                "working": False,
            }, None
        return {
            "status": "AVAILABLE" if payload else "AVAILABLE_EMPTY",
            "http_status": 200,
            "working": True,
            "record_count": len(payload),
        }, payload
    except Exception:
        return {"status": "REQUEST_FAILED", "working": False}, None


def quote_summary(quotes, now, max_age):
    result = {}
    for symbol in SAMPLES:
        quote = (quotes or {}).get(symbol)
        if not isinstance(quote, dict):
            result[symbol] = {"available": False}
            continue
        try:
            bid, ask = dec(quote["bp"]), dec(quote["ap"])
            timestamp = aware(quote["t"])
            if ask <= 0 or bid <= 0 or ask < bid:
                raise ValueError
            age = (now - timestamp).total_seconds()
            result[symbol] = {
                "available": True,
                "bid": str(bid),
                "ask": str(ask),
                "timestamp": timestamp.isoformat(),
                "fresh_for_entry": 0 <= age <= max_age,
            }
        except Exception:
            result[symbol] = {"available": False, "reason": "INVALID_QUOTE"}
    return result


def asset_inventory(assets):
    if not isinstance(assets, list) or not all(
        isinstance(a, dict) and a.get("symbol") for a in assets
    ):
        raise SafetyError("INVALID_ASSET_INVENTORY")
    active = [
        a
        for a in assets
        if a.get("class") == "us_equity" and a.get("status") == "active"
    ]
    tradable = [a for a in active if a.get("tradable")]
    exchanges = {}
    for asset in active:
        exchange = asset.get("exchange")
        label = exchange if exchange in EXCHANGES else "OTHER"
        exchanges[label] = exchanges.get(label, 0) + 1
    by_symbol = {a["symbol"]: a for a in active}
    samples = {}
    for symbol in SAMPLES:
        asset = by_symbol.get(symbol)
        samples[symbol] = {
            "active": asset is not None,
            "tradable": bool(asset and asset.get("tradable")),
            "exchange": (
                asset.get("exchange") if asset.get("exchange") in EXCHANGES else "OTHER"
            )
            if asset
            else None,
            "otc_excluded": bool(asset and asset.get("exchange") == "OTC"),
        }
    return {
        "complete": bool(active),
        "scope": "ACTIVE_US_EQUITY",
        "active_assets": len(active),
        "tradable_assets": len(tradable),
        "fractionable_assets": sum(bool(a.get("fractionable")) for a in active),
        "marginable_assets": sum(bool(a.get("marginable")) for a in active),
        "shortable_assets": sum(bool(a.get("shortable")) for a in active),
        "counts_denominator": "active US-equity assets; marginable/shortable are informational only",
        "exchange_distribution": exchanges,
        "otc_assets_excluded": exchanges.get("OTC", 0),
        "tradable_non_otc_assets": sum(a.get("exchange") != "OTC" for a in tradable),
        "samples": samples,
    }


def isolation_assessment(broker, store, now):
    positions = broker.positions()
    roots = broker.open_orders()
    from .reconcile import flatten

    orders = {
        o["id"]: o
        for o in flatten(roots)
        if o["status"] not in {"filled", "canceled", "expired", "rejected", "replaced"}
    }
    owned_positions, owned_ids, anomalies = {}, set(), []
    try:
        snapshot = reconcile(broker, store, now)
        anomalies = sorted({reason.split(":", 1)[0] for reason in snapshot.alerts})
        if snapshot.reconciled:
            owned_positions = snapshot.owned_positions
            owned_ids = {o["id"] for o in snapshot.owned_orders}
    except Exception:
        anomalies = ["OWNERSHIP_RECONCILIATION_FAILED"]
    unowned_positions = sum(p["symbol"] not in owned_positions for p in positions)
    unowned_orders = sum(oid not in owned_ids for oid in orders)
    has_external = bool(unowned_positions or unowned_orders)
    cobre_prefix = any(
        str(o.get("client_order_id", "")).lower().startswith(("cobre-", "cobre_"))
        for o in orders.values()
    )
    status = (
        "UNSAFE"
        if has_external or any(a != "ACCOUNT_NOT_BOUND" for a in anomalies)
        else "SAFE_WITH_LIMITATIONS"
        if anomalies
        else "SAFE"
    )
    return {
        "assessment": status,
        "scope": "observed account state only; exclusivity cannot be proven from a single snapshot",
        "position_count": len(positions),
        "open_order_count_including_legs": len(orders),
        "open_root_order_count": len(roots),
        "owned_position_count": len(owned_positions),
        "unowned_position_count": unowned_positions,
        "unowned_order_count": unowned_orders,
        "shared_account_detected": has_external,
        "shared_with_cobre_alpha": "yes" if cobre_prefix else "unknown",
        "cobre_assessment_basis": "client_order_id_prefix_inference"
        if cobre_prefix
        else "no_project_identity_proof",
        "anomalies": anomalies,
        "recommendation": "SEPARATE_PAPER_ACCOUNT"
        if status != "SAFE"
        else "VERIFY_EXCLUSIVE_ACCOUNT_BEFORE_ACTIVATION",
        "mutations": 0,
    }


def calendar_inventory(broker, now, schedule):
    day = now.astimezone(NY).date()
    rows = broker.calendar_range(day, day + timedelta(days=370))
    sessions = sorted((session_from_row(row) for row in rows), key=lambda s: s.open)
    if not sessions or any(s.open >= s.close for s in sessions):
        raise SafetyError("CALENDAR_VALIDATION_FAILED")
    today = next((s for s in sessions if s.trade_date == str(day)), None)
    early = next(
        (s for s in sessions if s.close - s.open < timedelta(hours=6, minutes=30)), None
    )

    def summary(session):
        return (
            {
                "trade_date": session.trade_date,
                "open": display_time(session.open),
                "close": display_time(session.close),
            }
            if session
            else None
        )

    early_status = "NOT_OBSERVED_IN_CALENDAR_RANGE"
    example = None
    if early:
        times = slot_times(early, schedule)
        valid = times["FORCE_FLAT"] == early.close - timedelta(minutes=45) and times[
            "RECONCILE"
        ] == early.close - timedelta(minutes=15)
        early_status = "PASS" if valid else "FAIL"
        example = {
            **summary(early),
            "force_flat": display_time(times["FORCE_FLAT"]),
            "reconcile": display_time(times["RECONCILE"]),
        }
    clock = broker.clock()
    return {
        "status": "PASS",
        "today": summary(today),
        "today_is_session": today is not None,
        "next_market_days": [summary(s) for s in sessions if s.trade_date > str(day)][
            :7
        ],
        "clock": {
            "is_open": bool(clock["is_open"]),
            "timestamp": display_time(aware(clock["timestamp"])),
        },
        "early_close_test": early_status,
        "early_close_example": example,
    }


def assert_validation_safety(store):
    strategy = store.read_strategy()
    switch = store.read_kill_switch()
    readiness = store.required("state/readiness.json").json()
    if (
        strategy.tradable is not False
        or switch.enabled is not True
        or readiness.get("execution_ready") is not False
    ):
        raise SafetyError("PHASE2_SAFETY_STATE_INVALID")
    return {
        "strategy_tradable": False,
        "kill_switch_enabled": True,
        "execution_ready": False,
    }


def run_diagnostics(broker, data, store, now, env=None):
    """Persist only genuine observations; never bind ownership or enable execution."""
    env = os.environ if env is None else env
    effective_paper_url = broker.verify_paper()
    safety_before = assert_validation_safety(store)
    account = broker.inspect_account()
    policy = store.read_policy()
    schedule = store.required("routines/schedule.json").json()
    rid = "connectivity_" + now.strftime("%Y%m%dT%H%M%S") + "_" + uuid4().hex[:12]
    path = ROOT + f"data/evidence/{rid}.json"
    report = {
        "schema_version": 1,
        "run_id": rid,
        "timestamp": now.isoformat(),
        "mode": "PAPER_ONLY",
        "paper_verified": True,
        "effective_broker_url": effective_paper_url,
        "validation_mode": "READ_ONLY",
        "safety_before": safety_before,
        "account": {
            "status": account["status"]
            if account["status"] in {item.value for item in AccountStatus}
            else "UNKNOWN",
            "cash": str(dec(account["cash"])),
            "equity": str(dec(account["equity"])),
            "buying_power": str(dec(account["buying_power"])),
            "trading_blocked": bool(account.get("trading_blocked")),
            "account_blocked": bool(account.get("account_blocked")),
        },
        "orders_submitted": 0,
        "orders_cancelled": 0,
        "positions_closed": 0,
        "broker_mutations": 0,
        "strategy_tradable": False,
        "kill_switch_enabled": True,
        "execution_ready": False,
        "errors": [],
    }
    for name, operation in (
        ("assets", lambda: asset_inventory(broker.assets())),
        ("isolation", lambda: isolation_assessment(broker, store, now)),
        ("calendar", lambda: calendar_inventory(broker, now, schedule)),
        (
            "slot_status",
            lambda: Runner(broker, store, data, NoAI(), lambda _: []).slot_status(now),
        ),
    ):
        try:
            report[name] = operation()
        except Exception:
            report[name] = {"status": "FAILED"}
            report["errors"].append(name.upper() + "_FAILED")
    samples = ",".join(SAMPLES)
    probes = {}
    snapshots = None
    for feed in ("iex", "sip"):
        result, quotes = probe(
            data,
            "/v2/stocks/quotes/latest",
            {"symbols": samples, "feed": feed},
            "quotes",
        )
        result["feed"] = feed
        result["samples"] = quote_summary(quotes, now, policy.max_data_age_seconds)
        result["data_observed"] = any(
            q["available"] for q in result["samples"].values()
        )
        probes[feed + "_quotes"] = result
    result, snapshots = probe(
        data, "/v2/stocks/snapshots", {"symbols": samples, "feed": "iex"}, None
    )
    result["feed"] = "iex"
    result["sample_availability"] = {
        s: isinstance((snapshots or {}).get(s), dict)
        and bool((snapshots or {})[s].get("latestQuote"))
        for s in SAMPLES
    }
    probes["iex_snapshots"] = result
    probes["sip_recent_bars"], _ = probe(
        data,
        "/v2/stocks/bars",
        {
            "symbols": "SPY",
            "feed": "sip",
            "timeframe": "1Min",
            "start": (now - timedelta(minutes=10)).isoformat(),
            "end": now.isoformat(),
            "limit": 1,
        },
        "bars",
    )
    probes["news"], _ = probe(
        data,
        "/v1beta1/news",
        {"symbols": samples, "limit": 1, "include_content": "false"},
        "news",
    )
    probes["most_active"], _ = probe(
        data,
        "/v1beta1/screener/stocks/most-actives",
        {"top": 10, "by": "volume"},
        "most_actives",
    )
    probes["movers"], _ = probe(
        data, "/v1beta1/screener/stocks/movers", {"top": 10}, "gainers"
    )
    report["data_probes"] = probes
    report["notes"] = [
        "IEX is not consolidated SIP.",
        "Closed-market quote age is reported separately from successful access; stale quotes cannot authorize entries.",
        "SIP entitlement means the exact latest/recent requests succeeded, not proof of real-time freshness outside market hours.",
        "Marginable and shortable are informational; the project stays LONG ONLY.",
    ]
    report["dry_run"] = dry_run()
    account_active = (
        report["account"]["status"] == "ACTIVE"
        and not report["account"]["trading_blocked"]
        and not report["account"]["account_blocked"]
    )
    iex_working = (
        probes["iex_quotes"]["working"]
        and probes["iex_quotes"]["data_observed"]
        and probes["iex_snapshots"]["working"]
        and any(probes["iex_snapshots"]["sample_availability"].values())
    )
    sip_entitled = (
        probes["sip_quotes"]["working"] and probes["sip_recent_bars"]["working"]
    )
    readiness = {
        "phase": 2,
        "validation_status": "OBSERVED",
        "paper_verified": True,
        "paper_connectivity_verified": True,
        "account_active": account_active,
        "market_data_working": bool(iex_working),
        "iex_working": bool(iex_working),
        "sip_entitled": bool(sip_entitled),
        "news_working": bool(probes["news"]["working"]),
        "asset_inventory_complete": bool(report["assets"].get("complete")),
        "shared_account_detected": bool(
            report["isolation"].get("shared_account_detected")
        ),
        "shared_account_isolation_safe": report["isolation"].get("assessment")
        == "SAFE",
        "notifications_configured": bool(
            (env.get("TELEGRAM_BOT_TOKEN") and env.get("TELEGRAM_CHAT_ID"))
            or env.get("DISCORD_WEBHOOK_URL")
        ),
        "strategy_tradable": False,
        "kill_switch_enabled": True,
        "execution_ready": False,
        "execution_enabled": False,
        "updated_at": now.isoformat(),
        "evidence_ref": path,
        "blockers": [
            "STRATEGY_NOT_TRADABLE",
            "KILL_SWITCH",
            "PRODUCTION_MUTATIONS_DISABLED",
            "EXIT_WATCHDOG_REQUIRED",
        ],
    }
    if not readiness["shared_account_isolation_safe"]:
        readiness["blockers"].append("ACCOUNT_ISOLATION_NOT_VALIDATED")
    if not readiness["market_data_working"]:
        readiness["blockers"].append("MARKET_DATA_NOT_VALIDATED")
    report["readiness"] = readiness
    report["commands"] = {
        "connectivity": "PASS",
        "inventory": "PASS" if report["assets"].get("complete") else "FAILED",
        "slot-status": "FAILED"
        if report["slot_status"].get("status") == "FAILED"
        else "PASS",
        "reconcile": "PASS"
        if not report["isolation"].get("anomalies")
        and report["isolation"].get("assessment")
        else "ATTENTION_REQUIRED",
        "dry-run": report["dry_run"]["status"],
    }
    broker.verify_paper()
    report["safety_after_reads"] = assert_validation_safety(store)
    report = clean_document(report, env)
    store.create_append_only_record(path, report)

    def update(latest):
        assert_validation_safety(store)
        if latest.get("execution_ready") is not False:
            raise SafetyError("PHASE2_SAFETY_STATE_INVALID")
        if latest.get("updated_at") and aware(latest["updated_at"]) > now:
            raise SafetyError("READINESS_NEWER_THAN_DIAGNOSTIC")
        return {**latest, **report["readiness"]}

    store.merge_projection("state/readiness.json", update)
    assert_validation_safety(store)
    return report
