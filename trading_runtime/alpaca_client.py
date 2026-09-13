"""Fixed Paper SDK adapter. Phase 1 has no production mutation implementation."""

from alpaca.trading.client import TradingClient
import requests
from alpaca.trading.enums import AssetClass, AssetStatus, QueryOrderStatus
from alpaca.trading.requests import (
    GetAssetsRequest,
    GetCalendarRequest,
    GetOrdersRequest,
    GetOrderByIdRequest,
)

from .config import PAPER_URL, SafetyError, verify_paper_url


class ReadOnlyPaperSession(requests.Session):
    """Enforce Phase 1 at HTTP transport too, including direct SDK calls."""

    def __init__(self):
        super().__init__()
        self.trust_env = False

    def request(self, method, url, **kwargs):
        if not str(url).startswith(PAPER_URL + "/v2/"):
            raise SafetyError("NOT_PAPER")
        if method.upper() != "GET":
            raise SafetyError("PHASE1_EXECUTION_DISABLED")
        kwargs["timeout"] = 20
        kwargs["allow_redirects"] = False
        return super().request(method, url, **kwargs)


class PaperAlpaca:
    def __init__(self, config):
        self._sdk = TradingClient(config.key, config.secret, paper=True, raw_data=True)
        self._sdk._session.close()
        self._sdk._session = ReadOnlyPaperSession()
        self.verify_paper()
        # Account status/identity must be the first network operation.
        self.inspect_account()

    def verify_paper(self):
        verify_paper_url(self._sdk._base_url)
        return PAPER_URL

    def _read(self, fn, *args, missing=False, **kwargs):
        self.verify_paper()
        try:
            return fn(*args, **kwargs)
        except Exception as exc:
            if missing and getattr(exc, "status_code", None) == 404:
                return None
            raise SafetyError("PAPER_BROKER_READ_FAILED") from None

    def inspect_account(self):
        """Read identity/status even when the account is not cleared to trade."""
        data = self._read(self._sdk.get_account)
        if not data.get("id") or not data.get("status"):
            raise SafetyError("PAPER_ACCOUNT_IDENTITY_MISSING")
        return data

    def account(self):
        data = self.inspect_account()
        if (
            not data.get("id")
            or data.get("status") != "ACTIVE"
            or data.get("trading_blocked")
            or data.get("account_blocked")
        ):
            raise SafetyError("PAPER_ACCOUNT_NOT_ACTIVE")
        return data

    def clock(self):
        return self._read(self._sdk.get_clock)

    def calendar(self, day):
        return self._read(
            self._sdk.get_calendar, GetCalendarRequest(start=day, end=day)
        )

    def calendar_range(self, start, end):
        return self._read(
            self._sdk.get_calendar, GetCalendarRequest(start=start, end=end)
        )

    def assets(self):
        return self._read(
            self._sdk.get_all_assets,
            GetAssetsRequest(
                status=AssetStatus.ACTIVE, asset_class=AssetClass.US_EQUITY
            ),
        )

    def positions(self):
        return self._read(self._sdk.get_all_positions)

    def open_orders(self):
        result = self._read(
            self._sdk.get_orders,
            GetOrdersRequest(status=QueryOrderStatus.OPEN, nested=True, limit=500),
        )
        if len(result) >= 500:
            raise SafetyError("ORDER_SNAPSHOT_INCOMPLETE")
        return result

    def order_by_client_id(self, client_id):
        order = self._read(self._sdk.get_order_by_client_id, client_id, missing=True)
        if order is None:
            return None
        return self._read(
            self._sdk.get_order_by_id, order["id"], GetOrderByIdRequest(nested=True)
        )

    def submit_bracket(self, **kwargs):
        self.verify_paper()
        raise SafetyError("PHASE1_EXECUTION_DISABLED")

    def cancel_order(self, order_id):
        self.verify_paper()
        raise SafetyError("PHASE1_EXECUTION_DISABLED")

    def close_owned(self, symbol, quantity, client_order_id):
        self.verify_paper()
        raise SafetyError("PHASE1_EXECUTION_DISABLED")
