"""Cached, rate-limited historical reads with explicit IEX provenance."""

import gzip
import hashlib
import json
from pathlib import Path
import time

import httpx

from trading_runtime.config import PAPER_URL, SafetyError, verify_paper_url

DATA_URL = "https://data.alpaca.markets"
ROUTES = {
    "bars": DATA_URL + "/v2/stocks/bars",
    "news": DATA_URL + "/v1beta1/news",
    "calendar": PAPER_URL + "/v2/calendar",
}


def digest(value):
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode()
    ).hexdigest()


class HistoricalSource:
    def __init__(self, config, cache, client=None, pause=time.sleep):
        verify_paper_url(PAPER_URL)
        self.cache = Path(cache)
        self.cache.mkdir(parents=True, exist_ok=True)
        self.client = client or httpx.Client(
            timeout=40, follow_redirects=False, trust_env=False
        )
        self.headers = {
            "APCA-API-KEY-ID": config.key,
            "APCA-API-SECRET-KEY": config.secret,
        }
        self.pause = pause
        self.requests = 0
        self.receipts = []

    def get(self, route, params):
        if route not in ROUTES or (route == "bars" and params.get("feed") != "iex"):
            raise SafetyError("RESEARCH_ROUTE_FORBIDDEN")
        key = digest({"route": route, "params": params})
        file = self.cache / (key + ".json.gz")
        if file.exists():
            with gzip.open(file, "rt", encoding="utf-8") as handle:
                body = json.load(handle)
        else:
            for attempt in range(3):
                if self.requests >= 2000:
                    raise SafetyError("RESEARCH_REQUEST_BUDGET")
                self.pause(0.4 if attempt == 0 else 2**attempt)
                self.requests += 1
                try:
                    response = self.client.get(
                        ROUTES[route], params=params, headers=self.headers
                    )
                except httpx.HTTPError:
                    if attempt == 2:
                        raise SafetyError("HISTORY_TRANSPORT_FAILED") from None
                    continue
                if response.status_code in {429, 500, 502, 503, 504} and attempt < 2:
                    continue
                if response.status_code != 200:
                    raise SafetyError("HISTORY_HTTP_" + str(response.status_code))
                body = response.json()
                with gzip.open(file, "wt", encoding="utf-8") as handle:
                    json.dump(body, handle)
                break
            else:
                raise SafetyError("HISTORY_TRANSPORT_FAILED")
        self.receipts.append(
            {"request_hash": key, "content_hash": digest(body), "route": route}
        )
        return body

    def bars(self, symbols, start, end, adjustment="raw"):
        params = {
            "symbols": ",".join(symbols),
            "start": start,
            "end": end,
            "timeframe": "5Min",
            "feed": "iex",
            "adjustment": adjustment,
            "asof": "-",
            "sort": "asc",
            "limit": 10000,
        }
        result = {s: [] for s in symbols}
        seen = set()
        while True:
            body = self.get("bars", params)
            for symbol, rows in body.get("bars", {}).items():
                if symbol not in result:
                    raise SafetyError("UNEXPECTED_HISTORY_SYMBOL")
                result[symbol].extend(rows)
            token = body.get("next_page_token")
            if not token:
                return result
            if token in seen:
                raise SafetyError("HISTORY_PAGINATION_LOOP")
            seen.add(token)
            params["page_token"] = token

    def audit_access(self):
        probes = []
        for start, end in [
            ("2016-01-04", "2016-01-09"),
            ("2020-01-06", "2020-01-11"),
            ("2023-01-03", "2023-01-07"),
            ("2025-01-06", "2025-01-11"),
        ]:
            try:
                rows = self.bars(["SPY", "QQQ"], start, end)
                probes.append(
                    {
                        "start": start,
                        "end": end,
                        "status": "AVAILABLE",
                        "symbols": {
                            s: {
                                "count": len(b),
                                "first": b[0]["t"] if b else None,
                                "last": b[-1]["t"] if b else None,
                            }
                            for s, b in rows.items()
                        },
                    }
                )
            except SafetyError as exc:
                probes.append({"start": start, "status": str(exc)})
        news = []
        for year in (2015, 2020, 2025):
            try:
                body = self.get(
                    "news",
                    {
                        "symbols": "AAPL",
                        "start": f"{year}-01-01",
                        "end": f"{year}-12-31",
                        "sort": "asc",
                        "limit": 1,
                        "include_content": "false",
                    },
                )
                items = body.get("news", [])
                news.append(
                    {
                        "year": year,
                        "count": len(items),
                        "status": "AVAILABLE" if items else "EMPTY",
                        "created_at": items[0].get("created_at") if items else None,
                        "updated_at": items[0].get("updated_at") if items else None,
                    }
                )
            except SafetyError as exc:
                news.append({"year": year, "status": str(exc)})
        calendar = self.get("calendar", {"start": "2020-01-01", "end": "2026-09-11"})
        return {
            "source": "Alpaca",
            "feed": "iex",
            "timeframe": "5Min",
            "adjustment": "raw",
            "asof": "-",
            "probes": probes,
            "news_probes": news,
            "calendar_sessions": len(calendar),
            "calendar_first": calendar[0]["date"] if calendar else None,
            "calendar_last": calendar[-1]["date"] if calendar else None,
            "request_count": self.requests,
            "receipts": self.receipts,
            "strategy_returns_computed": False,
            "historical_screener": "NOT_REPRODUCIBLE_FROM_CURRENT_ENDPOINTS",
            "point_in_time_stock_universe": "NOT_AVAILABLE",
            "effective_calendar_url": ROUTES["calendar"],
        }
