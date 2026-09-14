"""Capabilities describe verified scope, not marketing coverage."""

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class Capabilities:
    provider: str
    feeds: tuple[str, ...]
    supports_intraday: bool
    supports_corporate_actions: bool
    supports_delisted: bool
    supports_point_in_time: bool
    supports_news_revisions: bool
    evidence_level: str
    limitations: tuple[str, ...]


class MarketDataProvider(Protocol):
    capabilities: Capabilities

    def bars(self, symbols, start, end, feed, adjustment="raw"): ...


class CorporateActionsProvider(Protocol):
    def corporate_actions(self, symbols, start, end): ...


class SecurityMasterProvider(Protocol):
    def assets(self, status): ...


class HistoricalNewsProvider(Protocol):
    def news(self, symbols, start, end): ...
