"""IEX is explicit. SIP fallback records entitlement loss, never relabels data."""

import httpx

from .config import SafetyError

DATA_URL = "https://data.alpaca.markets"


class MarketData:
    def __init__(self, config, client=None):
        self._client = client or httpx.Client(
            timeout=20, follow_redirects=False, trust_env=False
        )
        self._headers = {
            "APCA-API-KEY-ID": config.key,
            "APCA-API-SECRET-KEY": config.secret,
        }

    def get(self, path, params):
        try:
            return self._client.get(
                DATA_URL + path, params=params, headers=self._headers
            )
        except Exception:
            raise SafetyError("MARKET_DATA_UNAVAILABLE") from None

    def snapshots(self, symbols, feed="iex"):
        if feed not in {"iex", "sip"} or len(symbols) > 50:
            raise SafetyError("INVALID_DATA_REQUEST")
        if not symbols:
            return {}, feed, []
        notes = []
        response = self.get(
            "/v2/stocks/snapshots", {"symbols": ",".join(symbols), "feed": feed}
        )
        if feed == "sip" and response.status_code in {401, 403}:
            feed = "iex"
            notes.append("SIP_ENTITLEMENT_UNAVAILABLE_FALLBACK_IEX")
            response = self.get(
                "/v2/stocks/snapshots", {"symbols": ",".join(symbols), "feed": feed}
            )
        if response.status_code != 200:
            raise SafetyError("SNAPSHOT_REQUEST_FAILED")
        return response.json(), feed, notes
