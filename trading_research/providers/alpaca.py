"""GET-only foundation adapter with fixed endpoints and pre-OOS price windows."""

from datetime import datetime, timezone
import time

import httpx

from trading_runtime.config import PAPER_URL, SafetyError, verify_paper_url
from ..data_quality import pre_oos_interval
from ..dataset_manifest import sha256
from .base import Capabilities

ROUTES = {
    "bars": "https://data.alpaca.markets/v2/stocks/bars",
    "quotes": "https://data.alpaca.markets/v2/stocks/quotes",
    "trades": "https://data.alpaca.markets/v2/stocks/trades",
    "corporate_actions": "https://data.alpaca.markets/v1/corporate-actions",
    "news": "https://data.alpaca.markets/v1beta1/news",
    "assets": PAPER_URL + "/v2/assets",
    "calendar": PAPER_URL + "/v2/calendar",
}


class AlpacaFoundationProvider:
    capabilities = Capabilities(
        "alpaca",
        ("iex", "sip"),
        True,
        True,
        False,
        False,
        False,
        "DOCUMENTED_PENDING_ACCOUNT_PROBES",
        (
            "feeds require separate entitlement verification",
            "delisted coverage incomplete until proven",
            "current assets are not a historical master",
            "original news revisions not guaranteed",
        ),
    )

    def __init__(
        self, config, cache, client=None, pause=time.sleep, request_budget=250
    ):
        verify_paper_url(PAPER_URL)
        self.headers = {
            "APCA-API-KEY-ID": config.key,
            "APCA-API-SECRET-KEY": config.secret,
        }
        self.client = client or httpx.Client(
            timeout=40, trust_env=False, follow_redirects=False
        )
        self.cache, self.pause = cache, pause
        self.requests, self.receipts, self.snapshots, self.memo = 0, [], [], {}
        self.action_version = "NOT_YET_AUDITED"
        if not 1 <= request_budget <= 20000:
            raise SafetyError("FOUNDATION_REQUEST_BUDGET_INVALID")
        self.request_budget = request_budget

    def get(self, route, params):
        if route not in ROUTES:
            raise SafetyError("FOUNDATION_ROUTE_FORBIDDEN")
        if route in {"bars", "quotes", "trades", "news", "corporate_actions"}:
            if not params.get("start") or not params.get("end"):
                raise SafetyError("FOUNDATION_EXPLICIT_INTERVAL_REQUIRED")
            pre_oos_interval(params["start"], params["end"])
        if route in {"bars", "quotes", "trades"} and params.get("feed") not in {
            "iex",
            "sip",
        }:
            raise SafetyError("FOUNDATION_FEED_REQUIRED")
        identity = sha256({"route": route, "params": params})
        if identity in self.memo:
            return self.memo[identity]
        for attempt in range(3):
            if self.requests >= self.request_budget:
                raise SafetyError("FOUNDATION_REQUEST_BUDGET")
            self.pause(1 if attempt == 0 else 2**attempt)
            self.requests += 1
            try:
                response = self.client.get(
                    ROUTES[route], params=params, headers=self.headers
                )
            except httpx.HTTPError:
                self.receipts.append(
                    {
                        "request_hash": identity,
                        "route": route,
                        "http_status": 0,
                        "attempt": attempt + 1,
                    }
                )
                if attempt < 2:
                    continue
                raise SafetyError("FOUNDATION_TRANSPORT_ERROR") from None
            self.receipts.append(
                {
                    "request_hash": identity,
                    "route": route,
                    "http_status": response.status_code,
                }
            )
            if response.status_code in {429, 500, 502, 503, 504} and attempt < 2:
                continue
            if response.status_code != 200:
                raise SafetyError("FOUNDATION_HTTP_" + str(response.status_code))
            body = response.json()
            self.receipts[-1]["content_sha256"] = sha256(body)
            self.memo[identity] = body
            return body
        raise SafetyError("FOUNDATION_TRANSPORT_ERROR")

    def freeze(self, domain, params, payload, route):
        config = {
            "source": "alpaca",
            "feed": params.get("feed", "reference"),
            "symbols": params.get("symbols", "").split(",")
            if params.get("symbols")
            else [],
            "universe_version": "V2A_FIXED_AUDIT_SAMPLES_NOT_A_STRATEGY_UNIVERSE",
            "start": params.get("start", "CURRENT_SNAPSHOT"),
            "end": params.get("end", "CURRENT_SNAPSHOT"),
            "timeframe": params.get("timeframe", "event"),
            "adjustment": params.get("adjustment", "not_applicable"),
            "retrieval_parameters": {"route": route, **params},
            "corporate_actions_version": self.action_version,
            "security_master_version": "CURRENT_ONLY_NOT_PIT",
        }
        meta = self.cache.freeze(domain, config, payload)
        self.snapshots.append(meta)
        return meta

    def paged(self, route, params, field, symbols=None):
        query = dict(params)
        combined = {} if symbols is not None else []
        seen = set()
        for _ in range(30):
            body = self.get(route, query)
            rows = body.get(field) or ({} if symbols is not None else [])
            if symbols is not None:
                for key, values in rows.items():
                    if route == "bars" and key not in symbols:
                        raise SafetyError("FOUNDATION_UNEXPECTED_SYMBOL")
                    combined.setdefault(key, []).extend(values)
            else:
                combined.extend(rows)
            cursor = body.get("next_page_token")
            if not cursor:
                return combined
            if cursor in seen:
                raise SafetyError("FOUNDATION_PAGINATION_LOOP")
            seen.add(cursor)
            query["page_token"] = cursor
        raise SafetyError("FOUNDATION_PAGINATION_INCOMPLETE")

    def bars(self, symbols, start, end, feed, adjustment="raw"):
        params = {
            "symbols": ",".join(symbols),
            "start": start,
            "end": end,
            "timeframe": "5Min",
            "feed": feed,
            "adjustment": adjustment,
            "asof": "-",
            "sort": "asc",
            "limit": 10000,
        }
        payload = self.paged("bars", params, "bars", symbols)
        self.freeze("market", params, payload, "bars")
        return payload

    def corporate_actions(self, symbols, start, end):
        params = {
            "symbols": ",".join(symbols),
            "start": start,
            "end": end,
            "limit": 1000,
            "sort": "asc",
            "data_quality": "all",
        }
        payload = self.paged("corporate_actions", params, "corporate_actions", symbols)
        self.action_version = sha256(payload)
        self.freeze("corporate_actions", params, payload, "corporate_actions")
        return payload

    def assets(self, status):
        if status not in {"active", "inactive"}:
            raise SafetyError("FOUNDATION_ASSET_STATUS")
        params = {"status": status, "asset_class": "us_equity"}
        payload = self.get("assets", params)
        self.freeze("security_master", params, payload, "assets")
        return payload

    def calendar(self, start, end):
        params = {"start": start, "end": end}
        payload = self.get("calendar", params)
        self.freeze("calendar", params, payload, "calendar")
        return payload

    def news(self, symbols, start, end):
        params = {
            "symbols": ",".join(symbols),
            "start": start,
            "end": end,
            "limit": 5,
            "sort": "asc",
            "include_content": "false",
        }
        body = self.get("news", params)
        allowed = (
            "id",
            "created_at",
            "updated_at",
            "headline",
            "source",
            "url",
            "symbols",
        )
        payload = [{k: row.get(k) for k in allowed} for row in body.get("news", [])]
        # Cache contains metadata only, never licensed full article text.
        self.freeze("news", params, payload, "news")
        return {
            "metadata": payload,
            "has_more": bool(body.get("next_page_token")),
            "original_revision_archive_verified": False,
            "retrieved_at": datetime.now(timezone.utc).isoformat(),
        }
